"""Phone-sized digest presentation and answer matching; no I/O or sends."""
from datetime import datetime, timedelta
import json
import re

import owed_routes


def instant(value):
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def as_of(events, now):
    return [e for e in events if instant(e['ts']) <= now]


def answered(row, request, events, now, sources=None):
    """A later answer closes the ask, including answers on another subject.

    Criterion-specific answers never close siblings. An explicit ref identifies
    only its referenced ask. Actor authorization is checked by the caller's fold.
    """
    since = instant(row['since'])
    refs = {r for r in (request.get('_src'), row.get('declared_src')) if r}
    sources = sources if sources is not None else {e['_src']: e for e in events if e.get('_src')}
    for e in events:
        kind = e.get('type')
        if kind not in {'decision', 'thinker-answer', 'owed-corrective', 'owed-met',
                        'owed-waived', 'owed-failed', 'operator-approval'}:
            continue
        if instant(e['ts']) > now:
            continue
        if kind in {'decision', 'thinker-answer', 'operator-approval'} and e.get('actor') not in ('thinker', 'operator'):
            continue
        if e.get('ref'):
            if e['ref'] in refs and instant(e['ts']) > since:
                return True
            # A ruling may answer the expiry/question for this check rather
            # than its most recent seat note. Keep that typed reference chain.
            target = sources.get(e['ref'], {})
            criteria = target.get('criteria') or []
            if isinstance(criteria, str):
                try:
                    criteria = json.loads(criteria)
                except ValueError:
                    criteria = []
            if target.get('criterion'):
                criteria = [target['criterion']]
            if target.get('type') == 'question' and not criteria:
                criteria = re.findall(r'\bAC\d+\b', str(target.get('asks', '')))
            if (target.get('subject') == row['spec'] and row['criterion'] in criteria
                    and instant(target['ts']) < instant(e['ts'])
                    and e.get('criterion') in (None, row['criterion'])):
                return True
            continue
        # Explicit operator consent does not expire when a seat repeats a note.
        consent = (e.get('actor') == 'operator' and e.get('type') in ('decision', 'operator-approval')
                   and (e.get('type') == 'operator-approval'
                        or re.search(r'\ball yes\b|\bapproval granted\b|\bapproved\b',
                                     str(e.get('why') or e.get('text') or ''), re.I))
                   and not re.search(r'not approved|awaiting approval', str(e.get('why', '')), re.I))
        if ((instant(e['ts']) > since or consent)
                and e.get('subject') == row['spec'] and e.get('criterion') in (None, row['criterion'])):
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
                  else 'Recommend granting only read access. No reply: check stays pending.')
    elif kind == 'real-world':
        ask = 'Handle the requested client email or purchase so work can continue'
        advice = 'Recommend reviewing it first. No reply: no external action.'
    else:
        if 'autovacuum' in text or 'scale_factor' in text:
            ask = 'Approve the Amazon fees table cleanup setting so its cleanup can be checked'
            advice = 'Recommend yes, in a quiet hour. No reply: setting stays off.'
        elif 'cron' in text or 're-enable' in text:
            job = ('the database timing sampler' if 'snapshot' in text else 'the saved-report refresh schedule')
            ask = f'Approve enabling {job} so its checks can collect evidence'
            advice = 'Recommend hold until the schedule is reviewed. No reply: stays off.'
        elif 'flag' in text:
            ask = 'Approve temporarily switching the production feature off to compare results'
            advice = 'Recommend yes, in a quiet test window. No reply: flag unchanged.'
        elif 'nginx' in text:
            ask = 'Approve the API server access configuration so we can verify its new access rules'
            advice = 'Recommend review before enabling. No reply: server unchanged.'
        elif 'restore' in text or 'tmpfs' in text or 'unshare' in text:
            ask = 'Approve an isolated live backup-restore drill so recovery can be checked'
            advice = 'Recommend yes, with supervision in a quiet window. No reply: drill waits.'
        elif 'write' in text or 'marks' in text:
            target = 'Koblenz' if 'koblenz' in text else 'live'
            ask = f'Approve one {target} completion test that saves a permanent record so we can verify it'
            advice = 'Recommend one supervised test. No reply: no record is written.'
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
