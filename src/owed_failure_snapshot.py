"""Read-only M2 reconciliation; never commissions, waives, closes or appends.

Run: python3 src/owed_failure_snapshot.py [--at ISO_UTC]
Output contains ledger references, not evidence bodies or waiver text.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json

import fold
import owed
import owed_verdicts

DEAD = {'killed', 'void', 'dropped', 'closed-unbuilt'}


def reference(event):
    if event is None:
        return None
    return {key: event.get(key) for key in ('type', 'ts', '_src')}


def snapshot(events, now):
    """One row for every admitted failed criterion, including undeclared rows.

    Rechecks and repeated failures do not reset the ownership clock. A later
    met or reasoned waiver closes an episode; a subsequent failure opens one.
    Existing active corrective links remain candidates, avoiding duplicate fixes.
    """
    events = [e for e in events if fold.ts(e.get('ts')) <= now]
    specs = fold.fold(events)[0]
    rows = []
    ignored_failures = sum(e.get('type') == 'owed-failed' and
                          fold.admits(e, e.get('actor', '')) is not None for e in events)
    for sid, spec in sorted(specs.items()):
        groups = defaultdict(list)
        statuses = {r['criterion']: r['status'] for r in owed.checks(spec['evs'], now)}
        for event in spec['evs']:
            if event.get('criterion'):
                groups[event['criterion']].append(event)
        for criterion, history in sorted(groups.items()):
            failures = [e for e in history if e.get('type') == 'owed-failed']
            if not failures:
                continue
            product = [e for e in failures if owed_verdicts.project_event(e)['type'] == 'owed-failed']
            capability = [reference(e) for e in failures if e not in product]
            verdicts = [e for e in history if e.get('type') in
                        ('owed-failed', 'owed-met', 'owed-unobservable', 'owed-waived', 'owed-unverifiable')]
            waivers = [e for e in history if e.get('type') == 'owed-waived'
                       and isinstance(e.get('reason'), str) and e['reason'].strip()]
            resolutions = [e for e in history if e.get('type') == 'owed-met'] + waivers
            resolved_at = max((fold.ts(e['ts']) for e in resolutions), default=None)
            episode = [e for e in product if resolved_at is None or fold.ts(e['ts']) > resolved_at]
            links = []
            for e in history:
                if e.get('type') == 'owed-corrective':
                    target = e.get('corrective')
                    state = specs.get(target, {}).get('state')
                    links.append(dict(reference(e), target=target, state=state,
                                      valid=state is not None and state not in DEAD | {'unknown'}))
            current_links = [link for link in links if link['valid'] and episode
                             and fold.ts(link['ts']) > fold.ts(episode[0]['ts'])]
            age = (now-fold.ts(episode[0]['ts'])).total_seconds()/3600 if episode else None
            active = bool(episode) and spec['state'] not in DEAD
            overdue = active and age >= 24 and not current_links
            waiver = waivers[-1] if waivers else None
            rows.append(dict(spec=sid, criterion=criterion, state=spec['state'],
                             status=statuses.get(criterion, 'undeclared'),
                             first_failure=reference(failures[0]),
                             first_product_failure=reference(product[0]) if product else None,
                             latest_verdict=reference(verdicts[-1]),
                             episode_first_failure=reference(episode[0]) if episode else None,
                             age_h=round(age, 6) if age is not None else None, open_product_failure=active,
                             capability_misclassifications=capability,
                             corrective_links=links, owned_by_corrective=bool(current_links),
                             waiver=(dict(reference(waiver), reason_present=True,
                                          reason_sha256=hashlib.sha256(waiver['reason'].encode()).hexdigest())
                                     if waiver else None),
                             overdue_unowned=bool(overdue), next_owner='thinker' if overdue else None,
                             action=('Link a valid existing corrective, commission through the authorized path, '
                                     'or obtain an explicit reasoned waiver; do not duplicate an existing fix.'
                                     if overdue else None)))
    return dict(at=now.isoformat(), reader='cached, read-only; no ledger/state writes',
                counts=dict(failed_criteria=len(rows),
                            capability_misclassifications=sum(len(r['capability_misclassifications']) for r in rows),
                            open_product_failures=sum(r['open_product_failure'] for r in rows),
                            overdue_unowned=sum(r['overdue_unowned'] for r in rows),
                            ignored_unauthorized_failures=ignored_failures), rows=rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--at', help='UTC observation time; defaults to now')
    args = parser.parse_args(argv)
    now = datetime.fromisoformat(args.at.replace('Z', '+00:00')) if args.at else datetime.now(timezone.utc)
    if now.tzinfo is None:
        parser.error('--at must include a timezone')
    print(json.dumps(snapshot(fold.read_events(), now), indent=2))


if __name__ == '__main__':
    main()
