# One actionable Telegram policy: 0051/R9 and 0058/R3

Delivery owner: `src/daily_summary.py`; interruption classification:
`src/notification_policy.py`. The Thinker owns ledger registration of both
charter obligations. This delivery does not file a spec or assert ledger coverage.

Existing work retained:

| Clause | Existing implementation and commits | Remaining seam closed here |
| --- | --- | --- |
| R9(a), current owed asks merged into the 06:00 UTC summary | W33 `digest_actions.py`; `aa575704` decision/approval classification; `ba5107db` denied-read triage; `26f797ff` concrete recommendations; `abf3585c` rulings linked to earlier questions | Enforce the inclusive 48-hour boundary; share the daily delivery record with stuck work; quietly acknowledge old, answered and wrong-owner rows. |
| R9(b), durable Thinker pane and three misses | `c36ec54d`, `remember-thinker-pane.sh`, flow bootstrap/reattach and flow snapshot | One alert per pane outage even as the backlog grows; healthy but slow answers never become phone alerts. Recovery rearms the watchdog. |
| R9(c), sender/time/first-line audit | `c36ec54d`, `scripts/tg-send.sh` | Both digest commands identify their shared sender as `daily-summary`. Fake curl proves audit records and restart deduplication without logging credentials. |
| R9(d), decisions, outages/money loss and one daily summary | W33 prose, answer matching and bounded formatting | Both commands build the same combined message. Severity-only maintenance, account rotations, runner repair and unreadable billing probes do not authorize a phone interruption. |
| 0058/R3, capability triage and actionable summaries | Same W33 commits above, plus 0049/R3 observation classification | Browser, read-only database and host-wrapper gaps go to Thinker/prod-runner and appear as counts. An operator label cannot promote a missing tool into an exclusive human act. |

Both existing production entrypoints remain in place:

    ./doit owed-digest --alert
    ./doit stuck-digest --send

Either command can deliver the combined summary in the 06:00 UTC hour. A shared
file lock and private `state/telegram-summary.json` serialize delivery and survive
restart. The second command stays silent. A successful legacy owed or stuck send
for today preserves today's limit during rollout. Transport failures do not claim
the day, so either entrypoint can retry. Detailed owed/stuck reports and their
existing ledger records remain available for the Thinker.

Only current positively classified operator acts enter NEEDS YOU. Later authorized
answers/verdicts suppress them; sibling criteria and unauthorized answers do not.
The phone message retains each displayed ask's recommendation, no-reply default
and reference within 1,000 characters. Deferred asks stay pending; silently retired
backlog does not consume the operator's attention.

Health alarms retain their measured severity and ledger evidence. Immediate
severity alone does not establish downtime or money loss; delivery requires a
positive `production_down` or measured `money_loss` observation. Actual engine
delivery outages (missing Thinker pane, missed core heartbeat, unreadable lane
rules, stopped merge dispatch) retain their existing outage route. Measured cost
threshold alarms and the monthly spending-limit decision retain their billing
route. Routine train adaptation, account rotation, missing usage reads and failed
runner switching remain maintenance follow-up. Unknown measurements never become
invented outages or operator approvals.

`test_daily_summary_policy.py` exercises both production commands in both orders,
the boundary/ownership/answer filters, retry, restart, read-only preview, real
sender wrapper with fake curl, maintenance versus observed impact, and pane
incident deduplication. Existing W33, operator-tool and formatting tests retain
pane persistence, full report content, source matching and transport audit proof.

A read-only real preview (including exact candidate asks) can be saved privately:

    DOIT_READ_CACHE=0 ./doit owed-digest --dry-run

Dry runs create no ledger, state or cache files and never invoke a sender. Outside
the 06:00 UTC hour their planned Telegram list is empty. The factory acceptance
record separately retains the combined proposal, real counts and exact source
asks; it is observation evidence, not authorization to send or grant access.
