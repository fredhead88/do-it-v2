---
spec_id: L-spec-1047
charter: L-charter-0051
project: albert-scott
unit: latency-check-https
requirement: R5
owed_rows: L-spec-0307 AC11
base_sha: "41f7e184fd"
requires_live_run: false
live_run_owner: builder
---

# spec: the daily portal latency check reaches the API over a loopback SSH tunnel, finds its key, and lands a `daily` event with `samples_ok`

charter: L-charter-0051
project: albert-scott

## Goal

The daily portal latency check (`portal_check.py --mode daily`, cron 06:30 UTC) changes from "fails every day at preflight `no-credential`, defaults to plain `http://167.71.46.51` so every request is `insecure-transport`, and has never landed a `daily` event" to "the daily wrapper opens an SSH loopback tunnel to the droplet API, fetches `API_KEY_READONLY` from the droplet, runs the check against `http://127.0.0.1:<port>` (a transport its own rule accepts), closes the tunnel, and appends one `daily` `latency-check` event a day, with `samples_ok`, to `L-latency-report-local.jsonl`".

In plain English: the daily speed check has never produced a result because it asks a plain-http address it refuses to use and cannot find its password; this spec has the daily job reach the API through a private tunnel, fetch the password from the server that holds it, and record how many requests really succeeded.

This supersedes the old spec's design (default `https://clients.albertscott.com`) and the Plan's revision-2 Goal on one point, the tunnel's remote port (Assumptions A2).

### Evidence (live read-only probe 2026-10-15, SD1; code at `origin/master` 41f7e184fd, SD12)

- `portal_check.py`: `_PROD_BASE_URL = "http://167.71.46.51"` is what `resolve_base_url` returns without `--base-url`. `_transport_allowed` accepts https or loopback only, so on the default every request is `{"error": "insecure-transport"}`. `_preflight` (daily only) checks credential, transport, `GET /health` (200 required), then `GET /api/health/version` with the key (200 or 404 pass).
- `https://clients.albertscott.com` is the Vercel dashboard: it serves no `/health` and takes only the session cookie. It is not a target. No TLS route to the droplet API exists.
- On the droplet (via `ssh root@167.71.46.51`, status codes only, no value printed):
  - `127.0.0.1:8000` (uvicorn, listens on loopback only): `/health` 200; `/api/health/version` 401 without a key and 200 with `API_KEY_READONLY` from the droplet's `/opt/albert-scott/.env`; `/stats` 200 with the key; the manifest endpoints `/api/v2/asin/list` and `/api/v2/profit/coverage-horizon` answer 401 without the key and 422 (routed, authenticated, parameters missing) with it.
  - `127.0.0.1:80` (nginx) with `Host: 127.0.0.1`: `/health` 404 and `/api/health/version` 404; with `Host: 167.71.46.51` `/health` 200. A request through a tunnel to :80 carries `Host: 127.0.0.1:<port>`, so port 80 answers 404.
  - The droplet's `.env` has one non-empty `API_KEY_READONLY` line (counted by name). This box's `/opt/albert-scott/.env` has none.
- `portal-check-daily-skips.log` shows `FAILED: portal-check-daily body exited rc=3` on 2026-10-11 to 2026-10-15. `/home/albert/.do-it/events/L-latency-report-local.jsonl` (the wrapper's `DOIT_LEDGER_FILE`) does not exist. The only `latency-check` events are `post-deploy` ones from `L-deployer-local.jsonl`.
- `doit append latency-check L-charter-0035 mode=daily ...` with `DOIT_LEDGER_FILE=<tmp>/events/L-latency-report-local.jsonl` is accepted (actor `latency-report`; `latency-check` has no `EMITS` row), so that file is a valid destination.
- `main()` calls `append_ledger` before the `ok_samples == 0` check and exits 4 afterwards, so an all-failed sweep still appends a `daily` event today.

Entry points (pre-flight 4). Covered: `--mode daily` through the cron wrapper (the tunnel path); a manual `--mode daily --base-url <b>`. Consciously unchanged: `--mode post-deploy` (from `deploy.sh` via `run_post_deploy_latency_check.sh`) keeps its old default and its inert behaviour (A5). Excluded: the hourly `latency_measure report --record` job (`portal_latency_check_wrapper.sh`), the crons.toml row (unit `latency-cron-row`, do-it-v2).

## Requirements

R5.1 `resolve_base_url(mode, base_url_arg)` (portal_check.py, existing) — Current: arg, else the `http://167.71.46.51` constant. Target: for `mode == "daily"` the precedence is the arg, then env `PORTAL_CHECK_BASE_URL` when non-empty, then the constant; for `post-deploy` the env is ignored and the result is the arg or the constant. Signature unchanged. Acceptance: AC1. review_path: pytest `-k ac1_base_url`.

R5.2 `samples_ok` on the event — Current: the daily event has `{mode, sha, clients, over_budget, p95_by_endpoint, report}`. Target: `build_ledger_argv` takes an optional `samples_ok` and, when given, appends `samples_ok=<int>`; `main()` passes the sum of `ok_samples` over non-skipped results for both modes. Event is still appended before the exit-4 decision (an all-failed sweep stays visible, with `samples_ok=0`). Acceptance: AC2. review_path: pytest `-k ac2_samples_ok`.

R5.3 Tunnel in `portal_check_daily_wrapper.sh` — Current: none. Target: a block between `# BEGIN tunnel-open` and `# END tunnel-open` picks a free loopback port, opens `ssh -f -N -M -S <ctl> -o BatchMode=yes -o ExitOnForwardFailure=yes -o ConnectTimeout=10 -L 127.0.0.1:<port>:127.0.0.1:8000 root@167.71.46.51` with the control socket in a `mktemp -d` directory, exports `PORTAL_CHECK_BASE_URL=http://127.0.0.1:<port>`, and installs an EXIT trap that runs `ssh -S <ctl> -O exit` and removes the directory. The ssh binary is `${PORTAL_CHECK_SSH:-ssh}`. If the tunnel does not open, the wrapper appends `[ts] TUNNEL-FAILED: ...` to the skips log and exits 3 without running the payload. Acceptance: AC3. review_path: pytest `-k ac3_tunnel`.

R5.4 Credential and paths in the wrapper — Current: `source /opt/albert-scott/.env` with a hardcoded path; `SKIPS_LOG` defined after the exports. Target: before the markers, `SKIPS_LOG="${SKIPS_LOG:-/var/log/albert-scott/portal-check-daily-skips.log}"` and `PORTAL_CHECK_ENV_FILE="${PORTAL_CHECK_ENV_FILE:-/opt/albert-scott/.env}"` are defined. The block between `# BEGIN credential-load` and `# END credential-load` contains the `.env` source (reading `${PORTAL_CHECK_ENV_FILE}`), then, while `API_KEY_READONLY` is empty: the optional `PORTAL_CHECK_CREDENTIAL_FILE`, then one fetch from the droplet (`${PORTAL_CHECK_SSH:-ssh} -o BatchMode=yes root@167.71.46.51` reading the `API_KEY_READONLY=` line of `${PORTAL_CHECK_REMOTE_ENV:-/opt/albert-scott/.env}`, surrounding quotes stripped). If still empty it appends `[ts] CREDENTIAL-MISSING: API_KEY_READONLY not set (looked in: <paths>)` and runs the payload anyway (it exits 3 `no-credential`). The value is never echoed. Acceptance: AC4. review_path: pytest `-k ac4_credential`.

R5.5 Installer warns when no credential source exists — Current: installs silently. Target: after the install `install_portal_latency_check_cron.sh` checks by name with `grep -q` (never printing a value) that `/opt/albert-scott/.env` or `PORTAL_CHECK_CREDENTIAL_FILE` has a non-empty `API_KEY_READONLY=` line, and otherwise prints `NOTE: API_KEY_READONLY not in a local file; the daily wrapper fetches it from the droplet over ssh`. It still exits 0. The daily cron line is not changed. Acceptance: AC5. review_path: pytest `-k ac5_installer`.

R5.6 The event lands — Current: no `daily` event has ever landed. Target: the real `append_ledger` path, in a temp `DOIT_ROOT` with `DOIT_LEDGER_FILE=L-latency-report-local.jsonl`, commits a `latency-check` line that a fresh process reads back with `doit events`. Acceptance: AC6 (hermetic), AC7 and AC8 (owed, production).

## Boundaries

In scope:
- `resolve_base_url` env handling and `samples_ok` plumbing in `portal_check.py`.
- The tunnel block, the credential block and the path variables in `portal_check_daily_wrapper.sh`.
- The credential note in `install_portal_latency_check_cron.sh`.
- New tests in `tests/health/test_latency_portal_check.py`; the existing `http://167.71.46.51` default assertions stay (they now pin the unchanged post-deploy and no-env behaviour).

Out of scope:
- `deploy/cron/portal_latency_check.cron` and the daily line: the line stays byte-identical so `latency-cron-row`'s `sig` still matches (seam).
- crons.toml and every do-it-v2 file: unit `latency-cron-row`, merged after this one.
- The droplet (nginx, uvicorn, sshd), DNS, any public route: the Goal forbids a droplet change.
- `run_post_deploy_latency_check.sh`, `deploy.sh`, `portal_manifest.toml`, `journal.py`, `__main__.py`: post-deploy is deliberately untouched (A5).
- Writing a key into any `.env`, `sudo` installs: operator steps.
- A ledger event on preflight failure: L-spec-0782 R2 decided a failed preflight appends nothing.
- The ledger filename `L-latency-report-local.jsonl`: shared with the hourly wrapper, pinned by `tests/health/test_latency_report.py`.
- Suppressing the event when `samples_ok` is 0: kept visible on purpose; the owed check reads the field.

## Interfaces

Consumes:
- `portal_check.resolve_base_url(mode: str, base_url_arg: str | None) -> str` (portal_check.py, existing)
- `portal_check._preflight(base_url: str) -> str | None` (existing, unchanged)
- `portal_check.append_ledger(argv, runner=None) -> None` (existing, unchanged)
- `/home/albert/do-it-v2/doit append|events` (through its CLI only)

Produces:
- The daily `latency-check` event `{mode: daily, sha, clients, over_budget, p95_by_endpoint, samples_ok}` (plus the existing `report`), subject `L-charter-0035`. `samples_ok` is one added field, an integer written as a string by `doit append`.
- `portal_check.build_ledger_argv(sha, mode, clients, over_budget, p95_by_endpoint_path, report_path, samples_ok=None) -> list[str]`.
- Seam to `latency-cron-row`: the daily line is unchanged: `30 6 * * * albert /bin/bash /opt/albert-scott/scripts/deploy/portal_check_daily_wrapper.sh`.

Writes:
- scripts/health/latency_measure/portal_check.py
- tests/health/test_latency_portal_check.py
- scripts/deploy/portal_check_daily_wrapper.sh
- scripts/deploy/install_portal_latency_check_cron.sh

## Constraints

- No `claude -p`, no `ANTHROPIC_API_KEY`, no paid call (SD13). No production write: no builder test touches `/home/albert/.do-it/events`, `/etc/cron.d`, the real droplet or the network; ssh is always the `PORTAL_CHECK_SSH` stub in tests.
- The key value never appears in stdout, stderr, a log line, a ledger argument, a report, or an ssh argument list. Tests assert this with a sentinel.
- The wrapper keeps these literals (pinned by `tests/health/test_latency_report.py`, outside `Writes:`): `export DOIT_ROOT=/home/albert/.do-it`, `export DOIT_LEDGER_FILE=/home/albert/.do-it/events/L-latency-report-local.jsonl`, `export PATH="/home/albert/do-it-v2:${PATH}"`, and no `cron_attest` text before the `export DOIT_ROOT=` line.
- `_transport_allowed` is not loosened. The tunnel's local end is `127.0.0.1` only (`-L 127.0.0.1:...`, never `0.0.0.0`).
- `portal_check.py` stays at or under 549 lines (534 today plus 15).

## Assumptions

- A1. Pre-flight 1: traced to charter L-charter-0051 R5 ("an event less than 26 h old") and owed row L-spec-0307 AC11, 2026-10-15. The charter says "using the https base"; the Plan's revision 2 supersedes that: no https base serves `/health` and takes the key, so the loopback tunnel (which `_transport_allowed` already accepts) is the transport.
- A2. Deviation from the Plan's example `-L ...:127.0.0.1:80`: the remote port is 8000. Observed 2026-10-15: through port 80 the Host is `127.0.0.1:<port>` and nginx answers 404 to `/health`; port 8000 answers `/health` 200 and the key works there (Evidence). It needs no droplet change and the same root ssh access. The Plan's own fallback (`ssh -L ... 127.0.0.1:8000`) is the audit's suggestion. If the Plan owner insists on :80, the checker would need a Host header, which is outside this unit's tested behaviour.
- A3. The key's home is the droplet's `/opt/albert-scott/.env` (one non-empty line, accepted by the API on `/api/health/version`, `/stats` and the manifest endpoints). The Plan's Q2 `charter-gap` does not fire: the API accepts the key. This box's `.env` has none; the wrapper fetches it over the same ssh path the box already uses (`deploy.sh` `SERVER=root@167.71.46.51`). `PORTAL_CHECK_CREDENTIAL_FILE` is an optional local override, unset by default.
- A4. The `albert` user's `~/.ssh/id_ed25519` reaches the droplet non-interactively (observed with `BatchMode=yes`, no agent). Cron has no `SSH_AUTH_SOCK`, so the wrapper relies on that key file only.
- A5. Post-deploy behaviour: unchanged on purpose. `run_post_deploy_latency_check.sh` passes no `--base-url` and the post-deploy mode has no preflight; it keeps the `http://` default and makes zero requests (`insecure-transport`), so the key is never sent anywhere. Ignoring `PORTAL_CHECK_BASE_URL` in that mode stops a stray env var from redirecting the key. A working post-deploy needs its own tunnel and is not this unit.
- A6. Pre-flight 2: no ADR in L-adr-0254..0273 applies to this footprint; git history shows L-spec-0782 (e0a2dd63e3, 2026-10-08) added the preflight and the transport rule and kept the default `http://` pending a TLS route. No open spec in the packet writes these four files.
- A7. Pre-flight 5, history (30 days): `e0a2dd63e3` 2026-10-08 (L-spec-0782) and `88a87f33e8` 2026-10-09 (PG attribution test fix) touch `portal_check.py` and its test; `18b4058646` 2026-10-14 touches none of the four. The two recent commits are in preflight and test regions, not the `resolve_base_url` and `build_ledger_argv` lines changed here. 0049's units touch do-it-v2 files only. Sibling `latency-cron-row` reads the daily line: merge order, this unit first.
- A8. Cost path: none (GET reads, ssh). Repro class: not a reproducible-logic bug; the failure is environmental. Hermetic tests reproduce the transport and event-shape halves; the tunnel, key and live host are `not-reproducible-here`, confirmed by AC7 and AC8.
- A9. Owed: 2 criteria (AC7, AC8), under the cap of 3.
- A10. Store round trip: AC6 runs the real `append_ledger` and reads back through a separate `doit events` process.
- A11. The tunnel is opened per run (not persistent), so no new daemon exists; if two runs overlap the existing `flock` in the wrapper already prevents it.

## Unknowns

- [NEEDS CLARIFICATION: whether the operator accepts the droplet's root ssh being used by cron to fetch `API_KEY_READONLY` at run time. Default: yes, as `deploy.sh` and the ops scripts already do the same; the alternative is the operator copying the line into this box's `.env`, which the wrapper already honours first.]

## rollback_path

Not required: no irreversible footprint (code, a shell wrapper and tests; no data layer). Revert is `git revert` of the merge commit.

## cost_path

None: no paid external call is fired by this footprint (GET reads of the droplet API over ssh).

## security_path

- Untrusted input: the base URL (arg or `PORTAL_CHECK_BASE_URL`) decides where the bearer key is sent. `_transport_allowed` still refuses everything but https or loopback, and the wrapper's tunnel end is loopback-only, so the key never crosses a network in clear text. The env override is read for `daily` only. The key is fetched into a shell variable, never printed, put in argv of any child, logged or written to a report; the wrapper's missing-credential line names paths only. The remote command is a fixed string (no interpolated input).
- Who may see this data: the key is a read-only API key held in the droplet's `/opt/albert-scott/.env` (root) and read by cron user `albert` through an existing ssh trust. The check reads timings and client ids already in the ledger event. Enforcement is the ssh key plus the API's `require_api_key`, which accepts the read-only key for safe methods only.

## Verification

```bash
cd "$(git rev-parse --show-toplevel)" && /opt/albert-scott/.venv/bin/pytest -q tests/health/test_latency_portal_check.py tests/health/test_latency_report.py && /opt/albert-scott/.venv/bin/ruff check scripts/health/latency_measure/portal_check.py tests/health/test_latency_portal_check.py && /usr/bin/bash -n scripts/deploy/portal_check_daily_wrapper.sh && /usr/bin/bash -n scripts/deploy/install_portal_latency_check_cron.sh && /usr/bin/grep -q 'samples_ok' scripts/health/latency_measure/portal_check.py && /usr/bin/grep -q '^30 6 \* \* \* albert /bin/bash /opt/albert-scott/scripts/deploy/portal_check_daily_wrapper.sh$' deploy/cron/portal_latency_check.cron && [ -z "$(git diff --name-only "$(git merge-base HEAD origin/master)" | /usr/bin/grep -v -x -e scripts/health/latency_measure/portal_check.py -e tests/health/test_latency_portal_check.py -e scripts/deploy/portal_check_daily_wrapper.sh -e scripts/deploy/install_portal_latency_check_cron.sh)" ] && echo VERIFY_OK
```

## Acceptance criteria

AC1 [backend]: `resolve_base_url("daily", None)` with `PORTAL_CHECK_BASE_URL` unset returns `http://167.71.46.51`; with it set to `http://127.0.0.1:41234` it returns that value; an explicit arg beats the env; an empty env value is ignored; `resolve_base_url("post-deploy", None)` returns `http://167.71.46.51` even with the env set to `http://127.0.0.1:41234`. A daily `main()` run with the env set to `http://127.0.0.1:41234`, a mocked `requests.get` and the sentinel key makes its two preflight calls to URLs starting `http://127.0.0.1:41234` and then runs the matrix; with the env set to `http://203.0.113.5` it returns exit 3 with `{"preflight": "failed", "reason": "insecure-transport"}` and makes zero `requests.get` calls. A post-deploy `main()` run with the env set and the mocked `requests.get` makes zero calls to the env's host. review_path: go to the repo root; run `pytest tests/health/test_latency_portal_check.py -k ac1_base_url`; worked if every case passes; failed if any request goes to a non-loopback `http://` URL, post-deploy follows the env, or the sentinel is printed.

AC2 [backend]: `build_ledger_argv(..., samples_ok=7)` contains `samples_ok=7` and the six existing fields unchanged in order; without the argument it contains no `samples_ok`. A daily `main()` run with stubbed results of `ok_samples` 5 and 3 appends exactly one argv beginning `latency-check L-charter-0035` containing `mode=daily` and `samples_ok=8`; with stubbed results of `ok_samples` 0 the argv contains `samples_ok=0` and the exit code is 4 (the event is still appended, then exit). review_path: run `pytest tests/health/test_latency_portal_check.py -k ac2_samples_ok`; worked if all cases pass; failed if `samples_ok` is missing, wrong, or the all-failed run exits 0.

AC3 [backend]: the text between `# BEGIN tunnel-open` and `# END tunnel-open`, run under `bash` with `PORTAL_CHECK_SSH` set to a stub that logs its argv and exits 0, exports `PORTAL_CHECK_BASE_URL` equal to `http://127.0.0.1:<port>` where `<port>` is the number inside the stub's logged `-L 127.0.0.1:<port>:127.0.0.1:8000` argument; the logged open call contains `-f`, `-N`, `BatchMode=yes`, `ExitOnForwardFailure=yes` and `root@167.71.46.51`; when the shell exits the stub log has a second call containing `-O exit` and the control directory no longer exists. With a stub that exits 255 on the open call, the block appends a line containing `TUNNEL-FAILED` to the file named by `SKIPS_LOG` and returns non-zero, and `PORTAL_CHECK_BASE_URL` is unset. review_path: run `pytest tests/health/test_latency_portal_check.py -k ac3_tunnel`; worked if both cases pass; failed if the local end is not `127.0.0.1`, the close call is missing, or a failed open still exports a base.

AC4 [backend]: the text between `# BEGIN credential-load` and `# END credential-load`, which contains the `.env` source, run under `bash -u` with `PORTAL_CHECK_ENV_FILE`, `SKIPS_LOG`, `PORTAL_CHECK_CREDENTIAL_FILE` and `PORTAL_CHECK_SSH` set to temp paths or the logging stub: (a) key present in the `.env` stand-in is exported and the stub is never called; (b) key absent there but present in the credential file is exported and the stub is never called; (c) key absent in both and the stub prints the sentinel for a command containing `API_KEY_READONLY` exports the sentinel; the stub's logged argv does not contain the sentinel; (d) key absent everywhere and the stub prints nothing appends one line starting `[` and containing `CREDENTIAL-MISSING: API_KEY_READONLY not set (looked in:` with the paths to the file named by `SKIPS_LOG`, and the sentinel appears in no captured stdout, stderr or log. In the full wrapper file, `SKIPS_LOG=` and `PORTAL_CHECK_ENV_FILE=` are defined before `# BEGIN credential-load`, no literal `source /opt/albert-scott/.env` remains, the four literals in Constraints are present, and `cron_attest` first appears after `export DOIT_ROOT=`. review_path: run `pytest tests/health/test_latency_portal_check.py -k ac4_credential`; worked if cases (a) to (d) and the static checks pass; failed if the block aborts under `set -u` before logging, writes to `/var/log`, or echoes a value.

AC5 [backend]: `install_portal_latency_check_cron.sh` contains, after the `install` step and before the final `echo`, a `grep -q` check on the name `API_KEY_READONLY` in `/opt/albert-scott/.env` and `PORTAL_CHECK_CREDENTIAL_FILE` that prints the `NOTE: API_KEY_READONLY not in a local file` text on absence, never exits non-zero because of it, and never prints a value; `deploy/cron/portal_latency_check.cron` still contains the line `30 6 * * * albert /bin/bash /opt/albert-scott/scripts/deploy/portal_check_daily_wrapper.sh` byte-for-byte. review_path: run `pytest tests/health/test_latency_portal_check.py -k ac5_installer`; worked if the static checks and `bash -n` pass; failed if the note or check is missing or the cron line differs.

AC6 [backend]: store round trip, no mocked write. A test sets `DOIT_ROOT` to a temp dir, `DOIT_LEDGER_FILE=<tmp>/events/L-latency-report-local.jsonl`, `PATH` starting with `/home/albert/do-it-v2`, and calls the real `portal_check.append_ledger(portal_check.build_ledger_argv(..., mode="daily", samples_ok=4))`; a separate `subprocess.run(["/home/albert/do-it-v2/doit", "events", "L-charter-0035"], env=...)` then prints one `latency-check` event whose line contains `"mode": "daily"` and `"samples_ok": "4"`, and the file `<tmp>/events/L-latency-report-local.jsonl` has exactly one line with no `actor` key. review_path: run `pytest tests/health/test_latency_portal_check.py -k ac6_round_trip`; worked if the fresh process prints the event and the file has one line; failed if the append raises, writes elsewhere, or the read-back is empty.

AC7 [observed-data]: OWED (production; elapsed time; deploy; ssh credential). Carried from L-spec-0307 AC11, narrowed as decided here: a real `latency-check` ledger event exists with `mode` `daily`, a `ts` within the last 26 hours, and `samples_ok` greater than 0, and every `over_budget` entry it names is cross-checked: its `client` is in the event's own `clients` list and its `endpoint`/`window`/`client` triple appears in the report JSON named by the event's `report=` field (the universe source, because `--dry-run` prints only mode, matrix size and clients, needs the production DB, and `offered_windows` uses `date.today()`; the dry-run re-run is therefore `not-reproducible-here` and not required). review_path: `doit events L-charter-0035 | grep latency-check | grep -F '"mode": "daily"' | tail -1`, then read the `report` file it names; worked if the line's `ts` is under 26 h old, `samples_ok` > 0, and every `over_budget` triple is in the report; failed or absent otherwise. This closes L-spec-0307 AC11.

AC8 [observed-data]: OWED (production; elapsed time). The event AC7 finds sits in `/home/albert/.do-it/events/L-latency-report-local.jsonl` (not only in `L-deployer-local.jsonl`), none of its `over_budget` entries has a `statuses` key `insecure-transport`, and `/var/log/albert-scott/portal-check-daily-skips.log` has no `FAILED: portal-check-daily body exited rc=` line of any rc, no `CREDENTIAL-MISSING` and no `TUNNEL-FAILED` line dated after the first post-merge 06:30 run. review_path: `grep -c latency-check /home/albert/.do-it/events/L-latency-report-local.jsonl`; `tail -1` of that file piped to `grep -c insecure-transport`; `tail` of the skips log; worked if the first is at least 1, the second is 0 and the log shows no such line after that run; failed otherwise.

## Declarations

- `{term: owed-ac, criterion: AC7, wake_at: 2026-10-17T08:00:00Z, line: "first 06:30 UTC run after merge and deploy: daily event under 26h old, samples_ok > 0, over_budget triples in the report (L-spec-0307 AC11)"}`
- `{term: owed-ac, criterion: AC8, wake_at: 2026-10-17T08:00:00Z, line: "same run: event is in L-latency-report-local.jsonl, zero insecure-transport entries, skips log has no FAILED/CREDENTIAL-MISSING/TUNNEL-FAILED line after it"}`
