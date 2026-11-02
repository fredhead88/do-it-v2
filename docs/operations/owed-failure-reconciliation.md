# Charter 0049 M2: reconcile failures, then observe the invariant

Owner: Thinker. Reuse the L-spec-1005 failure escalations and L-spec-1371
backfill work; apply the R3 capability classification before commissioning fixes.
This procedure does not authorize ledger appends, new specs, grants or waivers.

Take an uncached, read-only snapshot from this checkout:

    python3 src/owed_failure_snapshot.py > /private/audit-location/m2-before.json

The snapshot includes every admitted failed criterion, even one without a current
owed declaration. It lists the first failure and first product failure, latest
verdict and source, current failure episode and age, all corrective links and
target states, and a reasoned waiver's source and reason hash. Evidence bodies
and waiver text remain at their ledger references. Capability misclassifications
are counted separately. Rechecks and repeat failures do not reset the 24-hour
clock. A later met or reasoned waiver closes an episode; a later product failure
opens one. Unknown or killed corrective targets do not establish ownership.
A valid existing link still owns the episode after a repeated failure, so the
snapshot does not ask for duplicate corrective specs.

For each `overdue_unowned` row, inspect its sources and search existing fixes.
Through the normal authorized path, link a valid existing corrective, commission
one if none exists, or obtain an explicit, specific reasoned waiver. Record the
resulting source alongside the original failure reference in the reconciliation
record. A question, escalation, recheck, unknown target or blanket waiver is not
a completed corrective decision. Do not invent met verdicts. This script never
writes or applies these actions.

Take a fresh snapshot after the authorized reconciliation. Acceptance requires
`counts.overdue_unowned == 0`, with the per-row source mapping retained. Repeat
on the next day and retain both observation timestamps. An implementation test
or a dry-run count cannot establish those operational observations. Register
coverage through the Thinker's normal path; branch names and this document are
not ledger coverage.
