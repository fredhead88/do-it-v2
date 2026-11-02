"""Lossless capability observations at the sweeper write and historical read boundaries."""
import re

import owed_routes


_ERROR_ROUTES = (
    ('ro-dsn', r'permission denied for (?:relation|table|schema)\b|missing SELECT privilege|SELECT privilege (?:is )?missing'),
    ('browser', r'no (?:read-only |usable |available )?browser\b|browser (?:is )?(?:not available|unavailable)|browser tool.*(?:missing|absent)'),
    ('not-live', r'no deployed service\b|(?:service|application|target) (?:is )?not (?:yet )?deployed'),
    ('repo', r'(?:cannot|unable to) (?:read|open) (?:the )?(?:evidence|artifact)\b|(?:evidence|artifact).*\b(?:unreadable|not found)'),
    ('unknown', r'permission denied|access denied|missing (?:permission|privilege)'),
)


_SQL_TARGET = r'(?:"[^"\r\n]+"|[A-Za-z_][A-Za-z0-9_$]*)(?:\.(?:"[^"\r\n]+"|[A-Za-z_][A-Za-z0-9_$]*))*'


def _missing_access(text):
    access = [('USAGE on ' if match[1].lower() == 'schema' else 'SELECT on ') + match[2]
              for match in re.finditer(r'permission denied for (relation|table|schema)\s+(' + _SQL_TARGET + ')', text, re.I)]
    access.extend('SELECT on ' + match[1] for match in re.finditer(
        r'(?:missing SELECT privilege|SELECT privilege (?:is )?missing)(?:\s+(?:on|for))?\s+(?:relation\s+|table\s+)?(' + _SQL_TARGET + ')', text, re.I))
    return list(dict.fromkeys(access))


def normalize(row):
    """Positive inability evidence overrides a failed label; ordinary mismatches do not."""
    verdict = row.get('verdict') or {'owed-failed':'failed','owed-unobservable':'cannot-observe'}.get(row.get('type'))
    if verdict not in ('failed', 'cannot-observe'):
        return row
    text = str(row.get('why') or row.get('evidence') or '')
    detected = next((cap for cap,pattern in _ERROR_ROUTES if re.search(pattern,text,re.I)),None)
    uncertain = row.get('reason_code') in ('capability','undetermined') or re.search(
        r'no usable evidence|unable to classify (?:this|the) (?:result|observation)',text,re.I)
    # A demonstrated expectation/observation mismatch remains a product result,
    # even when the assertion names the access denial it expected to enforce.
    mismatch = re.search(r'\b(?:AssertionError|expected)\b[\s\S]*\b(?:observed|got|instead)\b', text, re.I)
    observed = re.split(r'\b(?:observed|got|instead)\b',text,maxsplit=1,flags=re.I)[-1]
    observed_gap = any(re.search(pattern,observed,re.I) for _,pattern in _ERROR_ROUTES)
    if verdict == 'failed' and mismatch and not uncertain and not observed_gap:
        return row
    if verdict == 'failed' and detected is None and not uncertain:
        return row
    capability = detected or row.get('capability') or 'unknown'
    value = dict(row,verdict='cannot-observe',capability=capability,why=text)
    if verdict == 'failed':
        value['original_verdict'] = 'failed'
    if 'capability' in row and row['capability'] != capability:
        value['original_capability'] = row['capability']
    access = row.get('missing_access') or []
    if not access and capability == 'ro-dsn':
        access = _missing_access(text)
    value['missing_access'] = access
    owner = row.get('next_owner')
    if owner not in ('thinker','prod-runner','operator'):
        owner = owed_routes.classify_ask(text,capability)['owner']
    value['next_owner'] = owner
    if not row.get('ask'):
        needs = '; '.join(access) or text or 'the missing tool/access'
        asks = {
            'ro-dsn': f'Thinker/prod-runner: use an approved read-only route with {needs}; otherwise request that exact access. {text if access else str()}',
            'browser': f'Thinker: supply a read-only browser runner/review session for this observation. {text}',
            'not-live': f'Thinker: identify the deployed service and deployment owner before retrying. {text}',
            'repo': f'Thinker: supply readable evidence or a capable repository runner. {text}',
            'droplet': f'Thinker: supply the approved read-only host wrapper. {text}',
            'elapsed': f'Thinker: name the observation window and its next due time. {text}',
            'operator-action': f'Operator: decide the stated action. {text}',
            'ro-dsn-grant': f'Operator: authorize the exact missing access: {needs}',
        }
        value['ask'] = (f'Thinker: identify the capable runner and observation path. {text}'
                        if owner != 'operator' and capability in ('operator-action','ro-dsn-grant') else
                        asks.get(capability,f'Thinker: classify capability {capability!r} and name its observation tool, access and owner. {text}'))
    return value


def project_event(event):
    """Interpret legacy observations without correcting or editing their source rows."""
    value = normalize(event)
    if value is event:
        return event
    value = dict(value,type='owed-unobservable')
    value.pop('verdict',None)
    return value


def events(results, batch):
    out = []
    for raw in results:
        row = normalize(raw)
        common = dict(criterion=row['criterion'],batch=batch,subject=row['spec'])
        if row['verdict'] == 'met':
            out.append(('owed-met',dict(common,evidence=row['evidence'])))
        elif row['verdict'] == 'failed':
            out.append(('owed-failed',dict(common,evidence=row['evidence'],kind=row['kind'])))
        else:
            fields = {key:row[key] for key in ('capability','why','ask','next_owner','missing_access',
                                               'evidence','original_verdict','original_capability') if key in row}
            out.append(('owed-unobservable',dict(common,**fields)))
    return out
