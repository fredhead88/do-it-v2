"""Phone-sized digest presentation and answer matching; no I/O or sends."""
from datetime import datetime, timedelta

import owed_routes


def instant(value):
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def as_of(events, now):
    return [e for e in events if instant(e['ts']) <= now]


def answered(row, request, events, now):
    """A later answer closes the ask, including answers on another subject.

    Criterion-specific answers never close siblings. An explicit ref identifies
    only its referenced ask. Actor authorization is checked by the caller's fold.
    """
    since = instant(row['since'])
    refs = {r for r in (request.get('_src'), row.get('declared_src')) if r}
    for e in events:
        kind = e.get('type')
        if kind not in {'decision', 'thinker-answer', 'owed-corrective', 'owed-met',
                        'owed-waived', 'owed-failed', 'operator-approval'}:
            continue
        if not since < instant(e['ts']) <= now:
            continue
        if kind in {'decision', 'thinker-answer', 'operator-approval'} and e.get('actor') not in ('thinker', 'operator'):
            continue
        if e.get('ref'):
            if e['ref'] in refs:
                return True
            continue
        if e.get('subject') == row['spec'] and e.get('criterion') in (None, row['criterion']):
            return True
    return False


def operator_line(row):
    """Translate positively classified acts; keep identifiers in the suffix."""
    text = str(row.get('ask', '')).lower()
    kind = row.get('kind') or owed_routes.classify_ask(text)['kind']
    if kind == 'decision':
        if 'guylian' in text and '13' in text:
            ask = 'Decide whether to retire 13 old Guylian import rows so cleanup can finish'
        else:
            ask = 'Decide which private-data access restrictions should remain so the policy check can finish'
        advice = 'Recommend keeping them until reviewed. No reply: unchanged.'
    elif kind == 'access':
        if 'github' in text or 'gh token' in text or 'runners api' in text:
            ask = 'Grant GitHub runner-status read access so we can check the build machines'
        elif 'supavisor' in text:
            ask = 'Provide an approved test connection pool so we can verify account isolation'
        else:
            subjects = [('ops_backup', 'backup records'), ('cron_client_outcomes', 'per-client import results'),
                        ('ops_request_health_buckets', 'request timing records'),
                        ('pg_stat_statements', 'database query timings'),
                        ('listing_slot_read_log', 'listing read history'),
                        ('amazon_sp_report_pending', 'pending Amazon reports'),
                        ('catalog', 'catalog records')]
            subject = next((name for token, name in subjects if token in text), 'protected database records')
            ask = f'Grant read access to {subject} so their checks can finish'
        advice = ('Recommend a separate test pool. No reply: test waits.' if 'supavisor' in text
                  else 'Recommend minimum read access. No reply: check stays pending.')
    elif kind == 'real-world':
        ask = 'Handle the requested client email or purchase so work can continue'
        advice = 'Recommend reviewing it first. No reply: no external action.'
    else:
        if 'autovacuum' in text or 'scale_factor' in text:
            ask = 'Approve the Amazon fees table cleanup setting so its cleanup can be checked'
            advice = 'Recommend a quiet hour. No reply: setting stays off.'
        elif 'cron' in text or 're-enable' in text:
            job = ('the database timing sampler' if 'snapshot' in text else 'the saved-report refresh schedule')
            ask = f'Approve enabling {job} so its checks can collect evidence'
            advice = 'Recommend reviewing the schedule first. No reply: stays off.'
        elif 'flag' in text:
            ask = 'Approve temporarily switching the production feature off to compare results'
            advice = 'Recommend a quiet test window. No reply: flag unchanged.'
        elif 'restore' in text or 'tmpfs' in text or 'unshare' in text:
            ask = 'Approve an isolated live backup-restore drill so recovery can be checked'
            advice = 'Recommend a supervised quiet window. No reply: drill waits.'
        elif 'write' in text or 'marks' in text:
            ask = 'Approve the live completion test that saves a permanent record so we can verify it'
            advice = 'Recommend a supervised test. No reply: no record is written.'
        else:
            ask = 'Approve the requested production check so verification can continue'
            advice = 'Recommend reviewing the change first. No reply: unchanged.'
    return f"{ask}. {advice} [{row['spec']}/{row['criterion']}]"


def trend(events, counts, now):
    yesterday = now.date() - timedelta(days=1)
    previous = [e for e in events if e.get('type') == 'owed-digest'
                and instant(e['ts']).date() == yesterday and isinstance(e.get('counts'), dict)]
    baseline = max(previous, key=lambda e: instant(e['ts']), default={}).get('counts', {}).get('by_status')
    return {name: counts.get(name, 0) - baseline.get(name, 0) if baseline is not None else None
            for name in ('met', 'failed', 'blocked')}


def counts_line(obj):
    c, delta = obj['counts']['by_status'], obj.get('trend', {})
    def change(name):
        value = delta.get(name)
        return f' ({value:+d} since yesterday)' if value is not None else ' (first daily baseline)'
    correcting = c.get('correcting', 0)
    return (f"Checks: {c.get('met', 0)} passed{change('met')}; "
            f"{c.get('failed', 0)} failing{change('failed')}, {correcting} fixes in progress; "
            f"{c.get('blocked', 0)} cannot run automatically yet{change('blocked')}.")


def pack(header, entries, footer=''):
    """Never cut an ask mid-sentence or drop its recommendation/default/ref."""
    text, included = header, 0
    for entry in entries:
        if len(text) + len(entry) + len(footer) + 90 >= 1000:
            break
        text += '\n' + entry
        included += 1
    if included < len(entries):
        text += f'\n{len(entries) - included} more asks in the full daily report.'
    if footer:
        text += '\n' + footer
    assert len(text) < 1000
    return text, included
