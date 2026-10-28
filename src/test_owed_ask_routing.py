#!/usr/bin/env python3
"""L-spec-1792: missing seat tools go to the Thinker; human acts stay put."""
import json
from pathlib import Path
import re

from owed_routes import classify_ask


# Frozen pre-change classifier: compare full results, including capability.
def base_classify_ask(note, capability=None):
    """Classify the act required, not the privileges of the reporting seat.

    Unknown/refused commands belong to the Thinker for triage. Only positive
    evidence of an exclusive human act goes to the operator. This does not
    grant a runner permission to execute anything.
    """
    text = ' '.join(str(note or '').lower().split())
    if re.search(r'operator(?:\W+\w+){0,3}\W+ruling|written operator ruling|decision, not an observable', text):
        return dict(owner='operator', kind='decision', capability='operator-action')
    # A denied SELECT proves only that this reporting seat cannot read it.
    # The Thinker may already have an approved route; do not demand a new grant.
    if (re.search(r'(?:operator|you)\s*:\s*(?:please\s+)?(?:give|grant|provide)\b', text)
            and not re.search(r'grant a batch|\bno (?:ro[ -]*dsn|review account)\b|'
                              r'grant (?:a |one |the )?(?:read-only )?dsn|'
                              r'provide the review account|no browser.*here', text)
            or 'authorized scratch supavisor' in text):
        return dict(owner='operator', kind='access', capability='ro-dsn-grant')
    if re.search(r'operator-gated|operator cron install|operator re-enables|operator-run write|'
                 r'flag toggle is a production change|sudo unshare.*live|tmpfs over the live|'
                 r'approve|approval|client email|purchase', text):
        kind = 'real-world' if re.search(r'client email|purchase', text) else 'approval'
        return dict(owner='operator', kind=kind, capability='operator-action')
    if re.search(r'not (?:yet )?deployed|not live|needs (?:a |integrator )?deploy|deploy has not run', text):
        route = 'not-live'
    elif re.search(r'elapsed|not yet due|next night|window cannot be complete|no deploy is in progress', text):
        route = 'elapsed'
    elif re.search(r'browser|review account|staff.key|authenticated|review credentials', text):
        route = 'browser'
    elif re.search(r'ro.dsn|ro.sql|database|dsn', text):
        route = 'ro-dsn'
    elif re.search(r'droplet|ssh', text):
        route = 'droplet'
    else:
        route = 'repo'
    return dict(owner='thinker', kind='follow-up', capability=route)


# The nine 2026-10-28 asks, verbatim; the first eight are tooling gaps.
ASKS = [
    ('0310', 'AC5', "value-replay-ab must run for every live client against the RO DSN; this batch has no read-only DB credential (capability=ro-dsn). Operator: grant the batch a read-only DSN.", 'ro-dsn'),
    ('0451', 'AC10', "read_model_equality must run against the deployed system; this batch has no read-only DB credential (ro-dsn), and the local Postgres cluster is absent. Operator: grant the batch a read-only DSN.", 'ro-dsn'),
    ('0662', 'AC6', "GitHub Actions API returns HTTP 403 for this seat's token (not accessible by personal access token), so the python-tests.yml master run and its baseline-gate summary cannot be read. Operator: grant Actions read or read the run summary.", 'repo'),
    ('0781', 'AC8', "Deploy time is not supplied and the log has no timestamps. cron_runs: 343 ok rows since 2026-10-09 (24/day, to 10-23), but failed rows at 10-09 00:55, 10-11 10:55, 11:55, 23:55 cannot be tied to unmeasured/record.skipped. Operator: give deploy time.", 'repo'),
    ('1048', 'AC5', "Roster live_db tests must run against prod Postgres read-only; no read-only DB credential for this batch, and running against a local cluster would not be the declared observation. Operator: provide a batch read-only DSN.", 'ro-dsn'),
    ('1353', 'AC9', "The Sales cell text for qur_life Sept 2026 must be read in a browser, and none is available. This batch also has no read-only DB credential, so the ro-sql aggregate was not run. Operator: provide a browser and the ro-dsn grant.", 'browser'),
    ('1409', 'AC16', "Needs a staff login on portal.albertscott.com to open the Koblenz L0.5 tab and save a revision. No browser and no staff review session exist in this batch. Operator: provide a staff verifier session.", 'browser'),
    ('1520', 'AC5', "gh run list against the albert-scott repo returns HTTP 403 (token cannot read Actions runs), so the CI run on merged master cannot be read. Operator: grant the gh token actions:read or check the CI run by hand.", 'repo'),
    ('1474', 'AC8', "ro-sql read of ops_host_samples returned permission denied for table: the read-only role has no SELECT grant on it. Operator: grant SELECT on ops_host_samples to the read-only role, or read both hosts' rows another way.", 'ro-dsn-grant'),
]


def check_ac1():
    for spec, criterion, note, route in ASKS[:8]:
        assert base_classify_ask(note)['owner'] == 'operator', (spec, criterion)
        for capability in (None, 'operator-action', 'ro-dsn-grant'):
            actual = classify_ask(note, capability)
            assert actual == dict(owner='thinker', kind='follow-up', capability=route), (spec, criterion, actual)
    # Every named tool, both markers, whole-text fallback, and word boundaries.
    tools = ('batch', 'actions:read', 'actions read', 'dsn', 'ro-dsn', 'browser',
             'chrome-devtools', 'dev server', 'review account', 'review session',
             'verifier session', 'staff login', 'deploy time')
    for tool in tools:
        expected = base_classify_ask(tool)
        for marker in ('Operator: ', 'You: ', ''):
            note = marker + 'provide ' + tool
            assert classify_ask(note) == expected, note
    for note in ('Browser unavailable. Operator: give protected production access.',
                 'Operator: provide browser. You: give protected production access.',
                 'You: provide DSN. Operator: give protected production access.',
                 'Operator: give seat access.', 'Operator: give token access.',
                 'Operator: provide batchwork.', 'Operator: give browsers.',
                 'Operator: give actions:reader.'):
        assert classify_ask(note) == base_classify_ask(note), note
        assert classify_ask(note)['owner'] == 'operator', note
    assert classify_ask('Operator: give protected access. You: provide browser')['owner'] == 'thinker'
    print('L-spec-1792 AC1 ok')


def check_ac2():
    real_grant = ASKS[-1][2]
    expected = dict(owner='operator', kind='access', capability='ro-dsn-grant')
    assert classify_ask(real_grant) == base_classify_ask(real_grant) == expected
    for privilege in ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'USAGE'):
        for marker in ('Operator: ', 'You: ', ''):
            note = 'No ro-dsn for this batch. ' + marker + f'grant {privilege} on ops_host_samples to the read-only role'
            assert classify_ask(note) == expected, note
    for approval in ('operator-gated', 'operator cron install', 'operator re-enables',
                     'operator-run write', 'flag toggle is a production change',
                     'sudo unshare the live', 'tmpfs over the live', 'approve',
                     'approval', 'client email', 'purchase'):
        for note in (f'Operator: provide browser; {approval}',
                     f'{approval}. Operator: provide browser'):
            assert classify_ask(note) == base_classify_ask(note), note
            assert classify_ask(note)['owner'] == 'operator', note
    note = 'Operator: approve granting the batch a read-only DSN'
    assert classify_ask(note) == base_classify_ask(note)
    assert classify_ask(note)['owner'] == 'operator'
    note = 'Needs a written operator ruling. Operator: provide browser'
    assert classify_ask(note) == dict(owner='operator', kind='decision', capability='operator-action')
    rows = json.loads((Path(__file__).parent / 'fixtures/digest-notes-2026-10-20.json').read_text())
    assert len(rows) == 37
    token_ask = next(row['note'] for row in rows
                     if row['spec'] == 'L-spec-1121' and row['criterion'] == 'AC8')
    assert classify_ask(token_ask) == expected
    for row in rows:
        for capability in (None, 'operator-action', 'ro-dsn-grant'):
            assert classify_ask(row['note'], capability) == base_classify_ask(row['note'], capability), row
    print('L-spec-1792 AC2 ok')


if __name__ == '__main__':
    check_ac1()
    check_ac2()
