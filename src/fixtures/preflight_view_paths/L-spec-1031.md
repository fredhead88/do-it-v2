# L-spec-1031 — runbooks-stale-commands

charter: null (free-standing, D15)
unit: runbooks-stale-commands
source: v4 spec https://github.com/fredhead88/albert-scott-platform/pull/721, carried to v2 form unchanged in substance
author: Yitzchak Greenblatt <yitzchak@ephraimgreenblatt.com> (cross-box thinker, think-remote; docs audit 2026-10-15)
base_sha: 9fd2a0547c3d56306d5edf471264221c71f4b000
code read at: audited commit 65e5d5a3f2e33fdf07ed1615e22f9990653a7d91 (origin/master, 2026-10-15)

## Goal

Four operator runbooks change from telling an operator to call a closed port, expect a response the API no longer returns, check the wrong server, or fill in a vault field the setup never reads, to stating the addresses, response shapes, hosts and field names the code and the boxes actually have.

In plain English: four operator runbooks tell people to call a port that is closed, expect an answer the API no longer gives, check the wrong server, or fill in a password-vault field the setup never reads; this spec corrects those lines and leaves the rest alone.

An audit of docs/runbooks against the code, the production database and live reads of both servers (2026-10-15) found the statements below wrong. Each is a command or expected result an operator would act on. The fix is text-only and touches four files. `docs/runbooks/client-offboarding.md` is deliberately not touched (an operator decision is pending on it) and `portal-account-provisioning.md` was checked and is correct.

Evidence, by requirement (code facts were read at the audited commit; live-box facts are dated and copied, see Assumptions):

- Upload reply (R1): `api/app/routers/upload_v2.py` declares `@router.post("/{report_type}", status_code=202, response_model=UploadAcceptedResponse)`. A fresh upload returns `status`, `report_ingestion_id` (int), `ingest_status` (`queued`), `raw_storage_path`, `file_sha256`, `message`; ingest runs in a background task. A duplicate hit (same agency, client, report type and file SHA-256) returns 202 with only `status`, `report_ingestion_id`, `ingest_status` and `message` ("File already registered (idempotent re-upload)..."), never `raw_storage_path` or `file_sha256` (upload_v2.py:465-483). The duplicate lookup does not filter on `ingest_status`, so a repeat of a file whose earlier ingest ended `failed` returns that same failed row and queues nothing. The module docstring (upload_v2.py:21-24) says the raw file stays on disk and "can be retried" and the reaper docstring and log line say "re-upload to retry" (upload_v2.py:262, 282); both are contradicted by the lookup order, and the doc must not repeat them. The column holding the hash is `report_ingestion.file_sha256` (the old runbook text says `report_ingestion.sha256`, which is wrong). `GET /api/v2/upload/status/{ingestion_id}?agency_id=&client_id=` returns `ingest_status` (`queued`, `ingesting`, `done`, `failed`), `ingest_error` and `lines_inserted`, which is the stored `row_count`, so it is 0 while the ingest is still `queued` or `ingesting`; a `done` ingest can also carry an `ingest_error` that the dashboard shows as "Note: ..." (upload-form.tsx:160-170), and `lines_inserted` can be 0 there too. The runbook shows `lines_inserted: 1234`, `skipped_duplicate`, `unknown_bucket_count` and a uuid id in the upload reply, none of which it returns. `unknown_bucket_count` still exists on the in-process `IngestionResult` (pipeline.py:54) but no HTTP reply carries it. The cited `recent_unknowns` function does not exist anywhere in the repository (`git grep -n recent_unknowns` over the whole tree finds only the runbook line that cites it; the guard below also checks `api/`); unrecognised fee triples are written to table `fee_bucket_unknown_log` (pipeline.py:359) and their lines get fee bucket `UNALLOCATED`, whose value is `"unallocated"` (fee_buckets.py:94), not `unknown_bucket`. The same bucket also receives the legacy alias `other` (fee_buckets.py:121), so lines in `unallocated` are a lead, not proof of an unknown triple; `fee_bucket_unknown_log` is the confirming table.
- Dashboard upload page (R1): `dashboard/src/components/profit-v2/upload/upload-form.tsx` (lines 150-185) shows "Ingesting... (<status>)", then "Ingest complete" with `lines_inserted` and any `ingest_error` note, or "Ingest failed" with the error; it has no "duplicates skipped" or "unknown buckets" figure (grep of the file for duplic/unknown: no match).
- Address (R2): the repo copy of the droplet's port-80 vhost, `config/nginx-api-internal.conf` (header: brought verbatim from `/etc/nginx/sites-enabled/api-internal` on 167.71.46.51, `listen 80; server_name 167.71.46.51`), sends `location /api/` to `http://127.0.0.1:8000` with `client_max_body_size 50M` and `proxy_read_timeout 300s` (lines 73-81), and `location /internal/` to the same upstream after stripping the `/internal` prefix, `proxy_read_timeout 90s`, with no body-size directive (lines 23-31); `location /` returns 403 (lines 87-89). That the API unit itself binds localhost only is a live-read fact (droplet, 2026-10-15), and the doc text does not mention worker counts. `deploy/nginx-bluedot.conf` is a different vhost (the bluedot HTTPS host; `config/nginx-bluedot.conf` is the other repo copy and its own header calls that vhost legacy and not in the traffic path; this spec cites the `deploy/` copy because only it carries the 500M location, `config/nginx-bluedot.conf` has no `client_max_body_size 500M` and also sends `/api/` to Vercel, 50M): its `location /api/v2/upload/` (lines 80-93) sets `client_max_body_size 500M` but its `proxy_pass` is `https://frontend-iota-seven-84.vercel.app` (Vercel), not the local API, and its `location /api/` (lines 95-105) is 50M, also to Vercel; only `/internal/`, `/webhook`, `/meetings` and similar go to `127.0.0.1:8000` there. So the 500M limit does not apply to `http://167.71.46.51/api/v2/upload/...`. By the repo copy, an upload through the droplet's port 80 is limited to 50M by nginx (413 above that); whether the live droplet vhost still matches the repo copy was not re-read, and the right operator address for uploads larger than 50M is not established. The runbook uses `http://167.71.46.51:8000`, not reachable from outside. The API is reachable through nginx on port 80 at `http://167.71.46.51`. That vhost has only `listen 80` (no TLS block in `config/nginx-api-internal.conf`), and `config/nginx-bluedot.conf` describes the same address as the backend Vercel calls over plain HTTP, so no HTTPS address for the droplet API is established in the repo and the `Authorization: Bearer` header travels unencrypted.
- Types (R3): `upload_v2.py` returns 410 for `settlement` when `is_cut_over(...)` is true (spec 640 R5, upload_v2.py:421); `is_cut_over` is true only when the client's row in table `settlement_spine_cutover` (columns `agency_id`, `client_id`, `state`) has `state = 'cutover'`; no row, `legacy`, `gated`, `rolled_back` and any database error all read as not cut over (settlement_cutover.py:47-111). The runbook's "Order of upload matters" section tells the operator to upload settlement second and to re-upload it after all_orders, which for a cut-over client is a 410. The route also accepts a fifth type `ads_monthly_statement` (PDF only, archive-only, no fact rows; `_ARCHIVE_ONLY_TYPES`). The runbook says four types, "You need all four" (line 27), "Upload all 4 report types" (line 176), and treats settlement as always available.
- Client ids (R4): the "Active client roster (as of 2026-05-26)" table lists four clients. The upload route takes `client_id` as a free form field and does not check it against `config/clients.json`, `config/amazon_clients.json` or any other roster (no reference to either file in `upload_v2.py` or `app/lib/profit_v2/ingestion/`, grep). `config/clients.json` is a JSON object with a `clients` key: the `clients` list holds the agency's 66 clients, each with a `name` slug such as `qur_life` (read from the repo file; 66 on the orchestration box too, live read 2026-10-15).
- Mirror host (R5, R6): `md_mirror` cron is installed on both the droplet and the orchestration box (live read 2026-10-15: droplet `/etc/cron.d/albert-scott-md-mirror` log shows "assembly complete"; the orchestration box assembles `/home/albert/md-mirror/` every 2 minutes). What the live read of 2026-10-15 tested on the orchestration box: a system-scope Syncthing unit was inactive and no `syncthing` process was listed. `scripts/setup-syncthing.sh` installs a systemd USER service (lines 80-92), so that test does not settle whether a user-scope Syncthing runs on either box; the Syncthing host is not known. The script says it installs Syncthing "on the box (Albert-Scott-Cloud)". `python scripts/spec_ledger.py --check` validates ledger records and says nothing about the mirror job. The job also records runs in `cron_runs` under job `md_mirror` (production read 2026-10-15). The runbook also says "the box" as if it were one known host at lines 9, 11, 19, 53, 57, 58 (device name `albert-scott-box`), 62, 66, 74 (same device name), 81, 89, 111, 122, 130, 132, 140 and 150; R5 and R6 cover all of them.
- Amazon Connect (R7 to R9): line 21 sends staff to `docs/playbooks/onboard-new-client.md` "sections 3.2 and 4"; that doc has no identity-registry, seller-account or financial-owner step; 3.2 is the hand-edit of `AMAZON_CLIENTS` in `client-switcher.tsx`, and section 4 (line 95 of the playbook) runs `scripts/backfill_*` commands that the sibling spec `playbooks-and-tuning-stale-a` records as nonexistent. The config files that exist are `config/amazon_client_identity.json`, `config/amazon_seller_accounts.json`, `config/amazon_financial_owners.json`, `config/amazon_client_registry_bridge.json`; what reads them (grep, audited commit): `pipelines/amazon/identity/manifest.py:40` reads the identity file, `pipelines/amazon/ams/handlers.py:94` and `scripts/amazon/backfill_ams_attribution.py` read the seller-accounts file, `pipelines/amazon/identity/financial_owners.py:35` reads the financial-owners file (its docstring calls it the only authority granting financial ownership of a seller's money) and `pipelines/amazon/identity/registry_bridge.py:41` reads the bridge file as an override table. Nothing in the repo states that a new client must have an entry in each, so the doc says a new client may need entries. The old runbook sentence (line 21) already tells staff to complete the identity registry, seller accounts, financial owners, client switcher and first-night backfill steps. The After-connect checklist in `docs/runbooks/amazon-connect.md` ("After-connect steps") already lists seven tracked steps per first connection (`api/app/lib/amazon_connect/followups.py:24-32`): `notifications_subscribed`, `first_backfill` and `cogs_seed` and `completeness_check` (automatic), `portal_login`, `ads_connection` and `brand_entity` (manual). `dashboard/src/lib/amazon-roster.ts` keeps a hardcoded fallback list `AMAZON_CLIENTS` and, for staff sessions only, merges the rows fetched from `/api/amazon-roster` over it (`useAmazonRoster`, lines 34-47); `client-switcher.tsx` calls `useAmazonRoster()`. The read-only SQL tool `ro-sql` exists only on the orchestration box (live read 2026-10-15), while the steps around it run against the droplet. The Niftyone token-sync section describes `/root/as-export/` and `NIFTYONE_*` settings; live read 2026-10-15 on the droplet: `/root/as-export` does not exist, `/opt/albert-scott/.env` has no `NIFTYONE_*` keys, the cron `/etc/cron.d/albert-scott-niftyone-token-sync` is installed, and every logged run ends with reason "missing NIFTYONE_SSH_TARGET" and zero sellers.
- Dev onboarding (R10, R11): `.env.dev.tpl:63` resolves `SUPABASE_DB_URL="bw://Albert Scott — Dev/SUPABASE_DB_URL_READONLY"`, so the field is `SUPABASE_DB_URL_READONLY` in the item `Albert Scott — Dev`; the runbook's 3a (line 66) calls `Albert Scott — Dev <name>` a collection, so the collection and the item are two different things and the new text must keep them apart. The runbook (line 68 and the sentence at lines 157-158, where the field name ends one line and "field." starts the next) says a field `READONLY` on a "Supabase item". The high-urgency rotation list (section 3d, lines 181-183) is the 2026-07-19 list of seven clients; `docs/architecture/credential_rotation_register.yml` exists and is the register the runbook already points to at line 204.

## Requirements

R1. In `docs/runbooks/profit-v2-report-ingestion.md`, replace the "Response shape" JSON block with the real upload reply, written as prose or a JSON block that begins with the literal line `HTTP 202`: the six fields of a fresh upload (`status`, `report_ingestion_id`, `ingest_status`, `raw_storage_path`, `file_sha256`, `message`), with the exact words "`report_ingestion_id` is an integer" on one line and `ingest_status` `queued`. Add a short step after the curl examples: poll `curl "$DROPLET/api/v2/upload/status/{report_ingestion_id}?agency_id=albert_scott&client_id=qur_life" -H "Authorization: Bearer $API_KEY"` until `ingest_status` is `done` or `failed` (a failed ingest carries `ingest_error`) and read `lines_inserted` from the status reply. Rewrite the "Deduplication behavior" paragraph so that it names the hash column `report_ingestion.file_sha256` (the old `report_ingestion.sha256` goes) and says: a repeat upload of the same bytes returns HTTP 202 with the existing `report_ingestion_id` and the message "File already registered"; that reply carries no `raw_storage_path` or `file_sha256`; and when the earlier ingest ended `failed` the repeat returns that failed row and does not re-run the ingest, so a failed ingest is not retried by re-uploading the same file (say this is the upload route's current behaviour at the audited commit, with the words `current behaviour`, not a guarantee; the lookup has no status filter and a later code change may alter it); the raw file stays on disk, but this runbook has no operator re-ingest procedure, so the operator reports the failure to the platform maintainers giving the `report_ingestion_id` and `ingest_error`. Say nothing that repeats the code docstring's "can be retried" or the log line's "re-upload to retry". Rewrite, to match, the troubleshooting row for `lines_inserted: 0` and the two other re-upload suggestions (the "re-uploading the same settlement file" sentence in "Order of upload matters" and the `Upload all_orders ... then re-upload settlement` fix in the `order_id_coverage_pct = 0` troubleshooting row): they must not tell the operator that re-uploading identical bytes re-ingests. The paragraph must no longer say "Safe to retry". The `lines_inserted: 0` row becomes: symptom "`lines_inserted: 0` in the status reply, no `ingest_error`", cause "the upload was a repeat of an earlier file (the upload reply said \"File already registered\", so the status reply shows the earlier ingest), the ingest has not finished (`ingest_status` is `queued` or `ingesting`), or the file held no lines", fix "check `ingest_status` first; if it is `done`, compare `report_ingestion_id` with the earlier upload (a repeat is not re-ingested)" (the old "Expected — already uploaded" and "duplicate file (SHA matches)" wording goes); the cause cell also covers a finished ingest whose status reply carries an `ingest_error` note (the dashboard shows it as "Note: ...") alongside a `lines_inserted` of 0. In "Order of upload matters", the sentence ending "re-uploading the same settlement file after all_orders lands" becomes a statement that settlement should be uploaded after all_orders because lines already ingested without ASIN/SKU attribution are not re-attributed by re-uploading identical bytes; give no recovery procedure. In Method 1 step 4, replace "The page shows ingest result: lines inserted, duplicates skipped, unknown buckets, errors." with a sentence saying the page shows the ingest status while it runs and then either "Ingest complete" with the number of lines inserted or "Ingest failed" with the error. Do not name `skipped_duplicate` anywhere afterwards, and the old response JSON block (`"error": null`, `"lines_inserted": 1234`) must be gone.

R2. In the same file, change the Method 2 `DROPLET` export to `http://167.71.46.51` (nginx, port 80) and add, in Method 2 on separate single lines: (a) as of 2026-10-15 live read, port 8000 is bound to localhost on the droplet and is not reachable remotely (state nothing about using `http://localhost:8000` from a shell: not evidenced); (b) by the repo copy of the droplet's port-80 vhost (`config/nginx-api-internal.conf`, not re-read on the droplet), `/api/` is forwarded to the local API with a 50M body limit, so a file over 50M is rejected by nginx with HTTP 413 on this address; (c) the 500M upload location in `deploy/nginx-bluedot.conf` belongs to the bluedot HTTPS host and forwards to Vercel, not to the droplet API, so it does not raise this limit, and the right address for files over 50M is not established: say, with the words `not supported by the documented path`, that files over 50M are not supported by the path this runbook documents and that no alternative is established (the 90-day history examples in this runbook may exceed 50M for a large client; the doc gives no workaround); (d) this address is plain HTTP, so `$API_KEY` travels unencrypted, and no HTTPS address for the droplet API is established. Do not say that 500M reaches the local API and do not recommend `/internal/` for uploads (the repo copy sets no body limit there).

R3. In the same file, rename the "The 4 report types" heading to `## The report types (four ingested types plus one archive-only type)` and add below the table: there is a fifth type, `ads_monthly_statement` (PDF only; archived, writes no fact rows). Replace "You need **all four**" with a sentence that says all four ingested types are needed for the full v2 picture and that `ads_monthly_statement` is optional. Say that `settlement` uploads return HTTP 410 for a client that has been cut over to the SP-API settlement feed (spec 640 R5), and say how to tell: a client is cut over when its row in table `settlement_spine_cutover` (matching `agency_id` and `client_id`) has `state = 'cutover'`; no row, `legacy`, `gated` and `rolled_back` mean it is not. In "Order of upload matters", add one sentence, using the words `step 2 is skipped`, that for a cut-over client settlement is not uploaded at all (HTTP 410), so step 2 is skipped for that client. The "Order" heading and list stay otherwise as they are. Add a troubleshooting row whose symptom cell begins `HTTP 410 on settlement`. Update the Troubleshooting "Use one of" list to `Use one of: settlement, all_orders, monthly_storage, advertised_product, ads_monthly_statement`. In the onboarding checklist, change step 2 to start `Upload all four ingested report types` and add that `settlement` is skipped for a client already cut over (it returns HTTP 410).

R4. In the same file, delete the "Active client roster (as of 2026-05-26)" heading, table and the "Add new clients by extending this table" sentence. Put in their place one short paragraph that says the upload route does not validate `client_id` against a roster, that the value to use is the client's `name` slug in the `clients` list of `config/clients.json` (a JSON object with a `clients` key), that each entry also has a `status` field (`active` for all 66 at the audited commit) and the operator should use the `name` of an `active` client, and that Qur Life (brand Beyoutiful) is `qur_life`. The paragraph must contain the words `status` and `active`. Do not state a client count in the doc. The onboarding checklist step 1 ("Add the `client_id` to the project's brand-mapping...") does not reference the deleted table and stays unchanged.

R5. In `docs/runbooks/laptop-mirror-setup.md`, rename the heading `## Prerequisites (box side — already done by orc)` to `## Prerequisites (box side)` and rewrite the Prerequisites so they state only what is known: the mirror is assembled by cron job `md_mirror` on the droplet (`167.71.46.51`) and on the orchestration box; one line beginning `The Syncthing host is unconfirmed` that says, as of 2026-10-15 live read, a system-scope Syncthing unit was inactive and no `syncthing` process was listed on the orchestration box, and that whether a user-scope Syncthing runs on either host was not established (the setup script installs a user service, which that test does not cover). Tell the operator to confirm the Syncthing host before Step 3 by running `systemctl --user is-active syncthing` and `ls /home/albert/md-mirror/RECENT.md` on the droplet and on the orchestration box (as the `albert` account: the cron runs as root with HOME=/home/albert, `scripts/md-mirror-assemble.sh` lines 7-8, and the setup script is written for `albert`, lines 4-8), and to take the device ID from the host where Syncthing is running, by opening that host's Syncthing web GUI and choosing Actions, Show ID. Replace the `bash /opt/albert-scott/scripts/setup-syncthing.sh` code block with one line saying not to run `scripts/setup-syncthing.sh` to look up the ID, because it installs and starts a Syncthing user service and is written for the `albert` account (script header, lines 4-8). Remove the sentence "Syncthing is running on the box as a systemd user service (send-only)". Cover every other place the file treats "the box" as one known host, with these replacements and no others: line 9 "written on the box" becomes "written on the host that assembles the mirror"; line 11 "The box is the sole source of truth" becomes "The Syncthing host is the sole source of truth"; "run this on the box to get it" becomes "read it on the confirmed Syncthing host to get it"; "the **box device ID**" and "Paste the box device ID" become "the **Syncthing host device ID**" and "Paste the Syncthing host device ID"; Step 3 heading "Add the box as a Syncthing device" becomes "Add the Syncthing host as a Syncthing device"; the suggested device name `albert-scott-box` (lines 58 and 74) becomes `albert-scott-mirror-host`; Step 3 "On the **box**" becomes "On the Syncthing host"; "show the box as a connected" becomes "show the Syncthing host as a connected"; "go back to the box" becomes "go back to the Syncthing host"; Step 4 `The box folder shows "Send Only"` becomes `The Syncthing host folder shows "Send Only"`; "written on the box" in Step 6 and "The box writes; you read" in Daily use become "written on the Syncthing host" and "The Syncthing host writes; you read"; the heading "Manual mirror refresh (box)" becomes "Manual mirror refresh (on the host that assembles the mirror)"; Security note "The Syncthing GUI on the box" becomes "The Syncthing GUI on the Syncthing host". After the edit the only remaining "box" in the file are "orchestration box", "each box" (R6) and the heading `(box side)`. Do not name either box as the Syncthing host.

R6. In the same file, make three Troubleshooting table edits. (1) In the `RECENT.md not updating` row, change the prefix `Box:` to `Host that assembles the mirror:` and keep the `tail -f` command. (2) In the `New files not appearing` row, change the prefix `Box cron running?` to `Mirror cron running?` and replace everything after it (the `python ... spec_ledger.py --check` text) with: run `tail /var/log/albert-scott/md-mirror.log` on the host that assembles the mirror and check the `md_mirror` row in the `cron_runs` table. (3) Leave the other rows unchanged. Separately, rename "Check box cron health" to "Check the mirror cron health" and replace its `ssh albert@167.71.46.51 "tail -20 ..."` command with the `tail -20 /var/log/albert-scott/md-mirror.log` command to be run in a shell on the droplet (`167.71.46.51`) or on the orchestration box, with a note that each box keeps its own `md-mirror.log`. Give no ssh user or target. Keep the manual refresh block `bash /opt/albert-scott/scripts/md-mirror-assemble.sh` unchanged: the script is `scripts/md-mirror-assemble.sh` in the repo and `deploy/cron/md_mirror.cron` runs it as `/opt/albert-scott/scripts/md-mirror-assemble.sh`, so that path is the deploy path on any host where the `md_mirror` cron is installed (both hosts, live read 2026-10-15); add one line under the block, containing `deploy path`, saying the block applies on a host that has the md_mirror cron installed and that the command is run as the account the cron uses (root, HOME=/home/albert).

R7. In `docs/runbooks/amazon-connect.md`, in "Invite and callback backend", replace only the sentence that sends staff to `docs/playbooks/onboard-new-client.md` "sections 3.2 and 4" with text that says: a new client may need entries in `config/amazon_client_identity.json`, `config/amazon_seller_accounts.json`, `config/amazon_financial_owners.json` and `config/amazon_client_registry_bridge.json` (the doc says the list names files that hold per-client entries and `does not claim to be exhaustive`, and does not claim each file is required); staff sessions merge the connected-client roster fetched from `/api/amazon-roster` into the client switcher list in `dashboard/src/lib/amazon-roster.ts` (`useAmazonRoster`), so for staff sessions a hand edit of `client-switcher.tsx` is not needed for the client to appear. That claim holds for staff sessions only: for non-staff sessions `useAmazonRoster` returns only the hardcoded list (`amazon-roster.ts:43`), and the list `AMAZON_CLIENTS` lives in `dashboard/src/lib/amazon-roster.ts` (`client-switcher.tsx` imports it), so say that a non-staff session still needs the client added to `AMAZON_CLIENTS` in `dashboard/src/lib/amazon-roster.ts`, using the words `non-staff`. Qualify it in the same sentence or the next: for a staff session the hardcoded `AMAZON_CLIENTS` fallback in `dashboard/src/lib/amazon-roster.ts` is the only list used when the roster fetch fails, and an entry in it wins over a fetched row with the same id (`merged.set` runs last), so a stale hardcoded entry would still show. Then point at the existing checklist: the post-connect steps (history backfill, default costs, portal login and the rest) are the tracked steps in "After-connect steps" in this runbook, which lists seven per first connection, and this section does not repeat or replace them (use the words `After-connect steps` and `first_backfill`, `cogs_seed` and `portal_login` on one line). Name no backfill script, no cron tier and no schedule for the first data pull (none is evidenced). Keep the following sentences (sibling marketplaces, token rotation) unchanged.

R8. In the same file, in "After-connect steps", replace the `/home/albert/do-it-v2/scripts/sweep/ro-sql` path with the wording "any read-only SQL session against the production database (as of 2026-10-15 live read, the sweep tool `ro-sql` exists only on the orchestration box)", on one line. Keep the SQL statement itself.

R9. In the same file, as the first paragraph under the "Niftyone token sync" section's `### Operator steps` heading, add one line beginning `Status as of 2026-10-15 live read:` saying that on the droplet `/root/as-export` does not exist, `/opt/albert-scott/.env` has no `NIFTYONE_*` keys, and each run of the installed cron ends with reason "missing NIFTYONE_SSH_TARGET" and zero sellers, so the sync is installed but not operating until those prerequisites are in place. Do not change the prerequisite instructions themselves.

R10. In `docs/runbooks/dev-onboarding.md`, replace the section 3a bullet "`READONLY` (Supabase item)" and the sentence in section 3c ("...into the dev collection's `READONLY` / field") with the field name `SUPABASE_DB_URL_READONLY`, and state in section 3a that `.env.dev.tpl` reads it as `bw://Albert Scott — Dev/SUPABASE_DB_URL_READONLY`, so the Bitwarden item name must be exactly `Albert Scott — Dev` (the template requires only this item name, `.env.dev.tpl:49-63`; the name of the collection that holds it is per-developer and the template does not read it). Keep the collection wording "Albert Scott — Dev <name>" (the template's own header, `.env.dev.tpl:34-36`, names the collection "Albert Scott — Dev" without a suffix; the runbook's per-developer suffix is existing wording the template does not read, so say in 3a, on one line with the words `does not read the collection name`, that the template reads only the item name) but make 3a consistent: the collection is shared to the new hire and holds one item named exactly `Albert Scott — Dev` (the template's `bw://Albert Scott — Dev/<FIELD>` reads fields from that item), and the fields listed in 3a belong to that item. In the same 3a bullets name the other fields the template reads from that item that the section describes only by vendor: `SCRAPINGBEE_API_KEY`, `APIFY_API_TOKEN`, `KEEPA_API_KEY` and `IMPORTGENIUS_API_KEY` (`.env.dev.tpl` lines 49-52), so 3a then lists every field the template reads: with these four the section names all thirteen `bw://Albert Scott — Dev/<FIELD>` fields (`SUPABASE_DB_URL_READONLY`, `API_KEY`, `WEBHOOK_SECRET`, `POSTHOG_WEBHOOK_SECRET`, `BETTER_AUTH_SECRET`, the four LLM keys and the four scraping/import keys), and the doc must not say the operator creates "exactly" a field set beyond that. No `READONLY` that is not part of `SUPABASE_DB_URL_READONLY` may remain in the file.

R11. In the same file, label the client list in the section 3d high-urgency rotation bullet as the `2026-07-19 snapshot` (on the same line as the word "snapshot") and add, on one line, the sentence "the current list of credentials to rotate is" followed by the register path `docs/architecture/credential_rotation_register.yml` in backticks. Keep the seven names.

R12. In `docs/runbooks/profit-v2-report-ingestion.md`, rewrite the `unknown_bucket_count` troubleshooting row: the symptom becomes "settlement lines with `fee_bucket = 'unallocated'` in `amazon_cash_pnl_lines`" with a note that the bucket also receives the legacy alias `other`, so it is a lead and `fee_bucket_unknown_log` confirms; the cause stays (a new fee category appeared, confirmed only when the log has `pending` rows for the client); and the fix replaces the `recent_unknowns` command with "look up the client's `pending` rows in `fee_bucket_unknown_log` (the service function `list_unknown_buckets` in `api/app/lib/profit_v2/services/fee_bucket_unknowns.py` reads that table, `pending` by default)". In the "What if a client's report formats look different?" section, change "rows get inserted as `unknown_bucket`" to "rows get inserted as `unallocated`". Neither `unknown_bucket_count` nor the backticked token `` `unknown_bucket` `` may remain in the file (`list_unknown_buckets` is required and is not that token).

## Acceptance Criteria

AC1 [backend]: `profit-v2-report-ingestion.md` shows the HTTP 202 fresh reply fields, the status-poll command and the `read lines_inserted` instruction in Method 2, the duplicate-upload behaviour (existing id, "File already registered", no `raw_storage_path`/`file_sha256`, failed row returned and not re-run) in "Deduplication behavior", and no longer contains `skipped_duplicate`, "Safe to retry", the `lines_inserted: 1234` sample or the uuid sample; its `lines_inserted: 0` row and Method 1 step 4 carry the rewritten text; "Order of upload matters" no longer says re-uploading re-ingests. review_path: the verification chain below.

AC2 [backend]: `profit-v2-report-ingestion.md` no longer exports `DROPLET` with port 8000, uses `export DROPLET="http://167.71.46.51"`, and in Method 2 carries the port-8000 note dated "as of 2026-10-15 live read" and the 50M limit and not-established lines (R2 b, c). review_path: the verification chain below.

AC3 [backend]: `profit-v2-report-ingestion.md` has the renamed report-types heading, names `ads_monthly_statement`, HTTP 410 and the `settlement_spine_cutover` / `state = 'cutover'` test in that section, tells a cut-over client to skip step 2 in "Order of upload matters", carries the 410 troubleshooting row and the five-type "Use one of" list, no longer contains the old heading, the old "all four" bold sentence or the old checklist step 2 wording (the exact strings are in the chain), and its checklist step 2 starts `Upload all four ingested report types`. review_path: the verification chain below.

AC4 [backend]: `profit-v2-report-ingestion.md` no longer contains "Active client roster", the "extending this table" sentence, or `unknown_bucket_count` or the backticked `unknown_bucket`, says the route does not validate `client_id` against a roster and points at `config/clients.json` in the section that replaced the table, and its `unknown_bucket_count` row now refers to `fee_bucket_unknown_log`. review_path: the verification chain below.

AC5 [backend]: `laptop-mirror-setup.md` has the heading `## Prerequisites (box side)`; its Prerequisites section says `The Syncthing host is unconfirmed`, names `systemctl --user is-active syncthing` and `ls /home/albert/md-mirror/RECENT.md`, is dated "as of 2026-10-15 live read" and names the droplet and the orchestration box; it says to read the device ID from the Syncthing web GUI, not by running `setup-syncthing.sh`; it no longer contains the sentence claiming Syncthing runs on the box as a user service, `albert-scott-box`, or any line with "box" other than "orchestration box", "each box" and the `(box side)` heading (a read-only Python check in the chain). review_path: the verification chain below.

AC6 [backend]: In `laptop-mirror-setup.md` the Troubleshooting section refers to `md_mirror` in `cron_runs`, names the droplet and the orchestration box with the per-box log note, has the renamed "Check the mirror cron health" block, and no longer contains `spec_ledger.py --check`, `ssh albert@167.71.46.51` or `Box: ` followed by `tail -f`. review_path: the verification chain below.

AC7 [backend]: In `amazon-connect.md` the "Invite and callback backend" section no longer contains `sections 3.2 and 4`, says a new client `may need` entries in the four config files, names `dashboard/src/lib/amazon-roster.ts` and `/api/amazon-roster`, qualifies the roster claim with the hardcoded `AMAZON_CLIENTS` fallback, points at the "After-connect steps" checklist (`first_backfill`, `cogs_seed`, `portal_login`), contains neither the word `T1` nor the word `T2`, and keeps the sentence "Do not create another client name for a seller already present"; the four config files and `amazon-roster.ts` exist in the repo. review_path: the verification chain below.

AC8 [backend]: `amazon-connect.md` no longer contains the path `do-it-v2/scripts/sweep/ro-sql`, and its "After-connect steps" section carries the replacement wording dated "as of 2026-10-15 live read". review_path: the verification chain below.

AC9 [backend]: In `amazon-connect.md` the "Niftyone token sync" section's `### Operator steps` heading is immediately followed by the paragraph `Status as of 2026-10-15 live read:` that mentions `/root/as-export`, `NIFTYONE_*` and `missing NIFTYONE_SSH_TARGET`. review_path: the verification chain below.

AC10 [backend]: `dev-onboarding.md` section 3a and section 3c name `SUPABASE_DB_URL_READONLY`, section 3a gives the `bw://Albert Scott — Dev/SUPABASE_DB_URL_READONLY` string and says the collection holds one item named exactly `Albert Scott — Dev`, no `READONLY` outside the longer name remains anywhere in the file, and section 3d labels the rotation list `2026-07-19 snapshot` and names `credential_rotation_register.yml`, and 3a says the template `does not read the collection name`, and names `SCRAPINGBEE_API_KEY`, `APIFY_API_TOKEN`, `KEEPA_API_KEY` and `IMPORTGENIUS_API_KEY`. review_path: the verification chain below.

AC11 [backend]: Only the four listed files changed (`docs/runbooks/client-offboarding.md` and everything else unchanged). review_path: the chain's name-only diff check passes on an empty diff, so it proves nothing before the edit; review_path: reviewer reads git diff --name-only.

AC12 [backend]: Every changed hunk lies inside the sections named in R1 to R12. review_path: the verification chain below runs a read-only per-file check that each `git diff -U0` hunk falls inside the allowed section ranges (that check also passes on an empty diff, so AC11 and AC12 are reviewer-backed); that the edits inside those sections match R1 to R12 sentence by sentence is `review_path: reviewer reads git diff` and the chain does not prove it. Likewise every phrase check in the chain proves only that a key phrase is present in its section, not that the surrounding sentence is correct; correctness of the new wording is reviewer-backed.

## Boundaries

In scope[]: the four runbook files above, text only.

Out of scope[]: `docs/runbooks/client-offboarding.md` (operator decision pending), `portal-account-provisioning.md` (checked, correct), any code, config, test or deploy file, and fixing the Syncthing host (no one has confirmed it).

### Not included

- Large report uploads: by the repo copy of the port-80 vhost a file over 50M gets HTTP 413 at `http://167.71.46.51`; R2 states that limit and states that the right address for larger files is not established (the 500M location in `deploy/nginx-bluedot.conf` forwards to Vercel). Choosing or building an upload path for files over 50M is not this spec's job, and the live droplet vhost was not re-read. R2 therefore tells operators that larger files are not supported by the documented path and that no alternative is established, although the runbook's 90-day history examples may exceed 50M.
- `docs/architecture/credential_rotation_register.yml` cites `dev-onboarding.md` by line (:180-190 at register line 36, :179-185 at line 52, :186-187 at line 127, :189-197 at line 149, :83 at line 190, :199-201 at line 239 and :199 at line 278); R10 and R11 add lines in sections 3a and 3d and shift those numbers. Not touched here (a Writes-disjoint file); a follow-up should refresh the line cites or move them to section names.
- Cleartext HTTP: R2 states that `http://167.71.46.51` carries the bearer token unencrypted and that no HTTPS address for the droplet API is established; adding TLS or an HTTPS upload address is not this spec's job.
- `profit-v2-report-ingestion.md` lines 6-16 ("Why this exists") and 131-145 ("Verifying the upload landed") were read for stale content: the `/api/v2/data-health` route and the `attribution_coverage` field exist (`data_health_v2.py`, `models.py:1292`), and the Plan 6 sentence is a forward-looking statement the audit could not check; no change, and those ranges stay outside AC12's allowed ranges.
- The 66 `config/clients.json` entries versus the 84 entries in the droplet's `amazon_clients.json`: R4 states no count and points at clients.json only; reconciling the two rosters is not this spec's job.
- Audit claim that every other `amazon-connect.md` module, script, env var and migration is wrong: the audit itself found them correct; no change.
- Droplet path `/var/lib/albert-scott/niftyone_token_sync` in `amazon-connect.md`: live read 2026-10-15 confirmed the directory exists (mode 700); no change.
- `config/amazon_client_survivor_overrides.json` and any other file under `config/` not listed in R7: whether a new client needs it was not checked; R7 does not claim its list is exhaustive.
- The `config/amazon_clients.json` mentions in `amazon-connect.md` and the rotation list in `dev-onboarding.md`: they name the live credential config, which exists on the droplet; no change.
- Whether `scripts/setup-syncthing.sh` itself should change: the runbook only runs it; out of scope.

## Interfaces

Consumes: none (text-only; no neighbour `Produces:` is read).

Produces: none (no code symbol, signature or schema changes).

Writes:
- docs/runbooks/profit-v2-report-ingestion.md
- docs/runbooks/laptop-mirror-setup.md
- docs/runbooks/amazon-connect.md
- docs/runbooks/dev-onboarding.md

## Constraints

- Text-only. No code, config, test or deploy file is modified.
- Every dated figure carries its date; the builder introduces no undated live-box figure and does not re-read the boxes. The builder copies the live-box statements in R2, R5, R8 and R9 as dated facts, with the wording "as of 2026-10-15 live read", and does not alter them. The file-existence and code facts (R1, R4, R7, R10) are checked by the builder against the repo, where the Verification chain has numbered checks for them.
- Each required phrase in the Verification chain must be kept on one line of the file, because the checks match single lines.
- The Verification chain assumes `origin/master` is fetched and present in the worktree; it uses `git merge-base origin/master HEAD` only as the base for the read-only diff checks (a stale local checkout does not move that base, a stale `origin/master` ref only widens the diff). The chain first fails visibly if `origin/master` is missing or does not contain the audited commit `65e5d5a3f`.
- The builder does not name either box as the Syncthing host and does not touch `docs/runbooks/client-offboarding.md`.
- No rollback_path, cost_path or security_path: the footprint is four text files, reversible by `git revert`, fires no paid external call, and touches no untrusted input, authentication or multi-client data.

## Assumptions

Live-box statements (droplet and orchestration box: the nginx/API bind, `md_mirror` cron, Syncthing state as tested (system-scope unit and process list on the orchestration box), `/root/as-export`, `NIFTYONE_*` keys, the Niftyone cron log, `ro-sql` location, the 66-client `clients.json` count on the orchestration box) come from a read-only live session on each host, 2026-10-15 about 13:00-13:25Z; the thinker keeps a transcript summary. The `md_mirror` row in `cron_runs` comes from a production read-only SELECT on 2026-10-15. The builder cannot re-read these and copies them as dated facts. Response shapes, status values and the 410 rule were read from `api/app/routers/upload_v2.py`, `api/app/lib/profit_v2/ingestion/pipeline.py` and `api/app/lib/profit_v2/fee_buckets.py` at the audited commit.

Audited against: `65e5d5a3f2e33fdf07ed1615e22f9990653a7d91` (origin/master, 2026-10-15).

Pre-flight record: (1) traces to no charter; free-standing per D15, source is the v4 spec above. (3) Every code and live-box fact above carries its file or its 2026-10-15 live-read date. (4) Entry points: the upload dashboard (Method 1) and the API curl (Method 2) are both covered by R1 to R4. (5) Concurrent work: the sibling spec `playbooks-and-tuning-stale-a` rewrites `docs/playbooks/onboard-new-client.md`, disjoint from this Writes. (6) No paid call. (7) Every criterion is verified at merge; none is owed. (8) Not a bug-fix spec.

## Decisions

- `client_id` source: the upload route validates nothing, so R4 says so and points at `config/clients.json` (the agency list with `name` slugs) rather than `amazon_clients.json` (a credentials config that the droplet holds with 84 entries as of 2026-10-15 live read). Reason: clients.json is committed and carries no secrets.
- A failed ingest: R1 says re-uploading identical bytes returns the failed row and does not retry, and tells the operator to report the id and error to the platform maintainers, instead of inventing a recovery procedure. Reason: the code docstring and reaper log say "retry" by re-upload but the duplicate lookup runs first and does not filter on status.
- Upload size: R2 states the 50M limit from the repo copy of the port-80 vhost and marks the larger-file address as not established, rather than pointing at the 500M location (it forwards to Vercel). Reason: that location does not reach the droplet API.
- Syncthing device ID: R5 tells the operator to read it from the Syncthing web GUI instead of running `setup-syncthing.sh`. Reason: the script installs and starts a service and expects the `albert` account, so running it on an unconfirmed host is a change, not a lookup.
- R7 qualifies "no hand edit needed" with the hardcoded fallback in `amazon-roster.ts`. Reason: the fallback wins over fetched rows and is the only list when the fetch fails.
- Dated live-read statements (R2, R5, R8, R9) are written as of 2026-10-15 and this spec does not keep them current.
- R6 gives no ssh command: the old command's user `albert` on the droplet is not evidenced. Reason: a wrong ssh target repeats the original defect.
- R12 rewrites the `unknown_bucket_count` row instead of leaving it: the field is not in any HTTP reply and the quoted command does not exist.
- R3 keeps the "four" counting but qualifies it as four ingested types, rather than rewriting the doc to five: only four types write fact rows.
- Cut-over test and 410 versus "Order of upload matters": R3 adds the `settlement_spine_cutover` test to the types section and a skip-step-2 sentence to the Order section, rather than deleting the ordering advice, which is still right for clients that are not cut over.
- Method 1 step 4 is fixed in R1 (the upload form shows status, lines inserted and error only), not left stale.
- Other "box" lines in `laptop-mirror-setup.md` (lines 9, 11, 19, 53, 57, 58, 62, 66, 74, 81, 89, 111, 122, 140, 150) and the device name `albert-scott-box` are covered by R5, because leaving "the box" or `albert-scott-box` would again name a host nobody confirmed. Replacement device name `albert-scott-mirror-host` is only a suggestion in the doc.
- Bitwarden: the collection is `Albert Scott — Dev <name>` and the item inside it is `Albert Scott — Dev`; R10 states both, since the template's `bw://` path uses the item name.
- The T1/T2 and first-data-pull claim is dropped from R7: it is not defined in the runbook and not evidenced.
- R7 limits "no hand edit needed" to staff sessions and names `amazon-roster.ts` (home of `AMAZON_CLIENTS`) for non-staff sessions, because `useAmazonRoster` merges the fetched roster for staff sessions only (`amazon-roster.ts:43`).

- Archive-only reply: the duplicate lookup in `upload_v2.py` (465-470) runs for all five report types; `_ARCHIVE_ONLY_TYPES` (line 437) is only the PDF magic-byte check before it and the archive-only branch (line 317) sits in the background task after it, so R1's dedup text applies to `ads_monthly_statement` too and no separate note is added.
- R7 says "may need" and cites what reads each file, because the repo shows readers but no rule that every new client needs every file; it points to the existing After-connect checklist (seven tracked steps, `followups.py:24-32`) instead of listing post-connect steps itself.
- R2(a) no longer claims `http://localhost:8000` works from a droplet shell: nothing evidences it.
- The heading `(box side)` in `laptop-mirror-setup.md` is kept as is (R5 renames it from "already done by orc" only); the device name `albert-scott-mirror-host` is a suggestion.
- The spec could be split into one spec per runbook file; kept as one because the four edits share a verification chain and are small.
- R5 uses the absolute path `/home/albert/md-mirror/RECENT.md`, not `~`: the cron runs as root with HOME=/home/albert, so a `~` path differs by account.
- R2 cites `deploy/nginx-bluedot.conf` for the 500M location because `config/nginx-bluedot.conf` has no such location; either way it forwards to Vercel.
- R2 states the cleartext-HTTP caution but does not recommend an alternative, because no HTTPS address for the droplet API is established.
- Live-box facts (R2, R5, R8, R9) stay in the permanent runbooks as dated text, "as of 2026-10-15 live read": the date and source travel with the statement, so a later reader can tell it is a snapshot, and operators need the current state of those prerequisites now.
- The Syncthing host stays an open item (R5 and Unknowns say so plainly, and the doc names neither box): no read established it, and guessing would repeat the original defect.
- R4 tells operators to use an active client's `name`: `config/clients.json` entries carry a `status` field (`name`, `display_name`, `status` and others); the route validates nothing, so the doc points at the roster rather than promising validation.
- R10 keeps the runbook's collection wording and states the template header differs ("Albert Scott — Dev" without a suffix): the template reads only the item name, so the collection name is not a requirement, and 3a now lists all thirteen fields the template reads rather than claiming a narrower set.
- R6 keeps the manual-refresh path `/opt/albert-scott/scripts/md-mirror-assemble.sh`: `deploy/cron/md_mirror.cron` runs that exact path; the repo script is `scripts/md-mirror-assemble.sh` (also present as a copy under `do-it-starter/scripts/`).
- R12 covers the `unknown_bucket` mention in "What if a client's report formats look different?" (runbook line 169, inside the allowed range), and the chain bans the token file-wide.
- The failed-ingest text is worded as the route's current behaviour at the audited commit: the duplicate lookup has no status filter, which may be a code defect; the runbook does not promise it.
- The chain's phrase checks prove presence, not correctness; sentence-level correctness is reviewer-backed (AC12).

## Unknowns

[NEEDS CLARIFICATION: whether the live droplet vhost still matches `config/nginx-api-internal.conf`, and which address takes uploads over 50M.]
[NEEDS CLARIFICATION: which host runs the laptop-mirror Syncthing, and whether either box has a user-scope Syncthing (R5 marks it unconfirmed on purpose).]
[NEEDS CLARIFICATION: how an operator re-runs an ingest that ended `failed` (R1 says only that identical-bytes re-upload does not).]

## Verification

```bash
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
B="$(git merge-base origin/master HEAD)" &&
/usr/bin/python3 -I -c 'import sys,subprocess as s;o=set(s.check_output(["git","diff","--name-only",sys.argv[1]],text=True).split());assert o<=set(sys.argv[2:]),o' "$B" $P $L $C $D &&
H='import sys,re,subprocess as s,functools as F;b,f=sys.argv[1:3];L=open(f,encoding="utf-8").read().split("\n");fd=lambda p,c:next(k for k in range(c,len(L)) if L[k].startswith(p));rg=lambda sp:(lambda st,en:(lambda c:(c+1,fd(en,c+1) if en else len(L)))(F.reduce(lambda c,p:fd(p,c),st.split(">>"),0)))(*sp.split("|"));R=[rg(x) for x in sys.argv[3:]];d=s.check_output(["git","diff","-U0",b,"--",f],text=True);ck=lambda m:(lambda lo,hi:any(x<=lo and hi<=y for x,y in R) or sys.exit("hunk outside: %s %d-%d"%(f,lo,hi)))(int(m.group(1)),int(m.group(1))+max(int(m.group(2) if m.group(2) else 1),1)-1);[ck(m) for m in re.finditer(r"(?m)^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@",d)]' &&
/usr/bin/python3 -I -c "$H" "$B" $P '## The|## Upload methods' '### Method 1|### Method 2' '### Method 2|## Recommended download cadence' '## Order of upload matters|## Verifying the upload landed' '## Troubleshooting|## What if' '## What if|## Active client onboarding checklist' '## Active client onboarding checklist|' &&
/usr/bin/python3 -I -c "$H" "$B" $L '|## Step 1' '## Step 3|## Step 5' '## Step 6|' &&
/usr/bin/python3 -I -c "$H" "$B" $C '## Invite and callback backend|### How to check it' '## Niftyone token sync>>### Operator steps|### How to check it' '## After-connect steps>>### How to check it|### Rollback' &&
/usr/bin/python3 -I -c "$H" "$B" $D '### 3a.|### 3b.' '### 3c.|### 3d.' '### 3d.|## 4.' &&
echo VERIFY_OK
```

The chain assumes `origin/master` is fetched; AC11 and AC12 pass on an empty diff and are reviewer-backed (`git diff` read sentence by sentence against R1 to R12). Its `guard` checks are true before and after the edit.

## Dependencies

None to merge. `playbooks-and-tuning-stale-b-spec.md` (line 35) cites the ingestion runbook's `settlement` row only as an audit fact and does not write the file. R7 refers staff to config files rather than to `docs/playbooks/onboard-new-client.md`, which the separate spec `playbooks-and-tuning-stale-a` rewrites; no other spec in `~/spec-drafts/docs-corrections-2026-10-15/` lists any of these four runbook paths in its Writes (checked by grep over the folder, 2026-10-15).
