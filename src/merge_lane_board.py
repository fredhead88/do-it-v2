"""Read-only board projection of queued batches and their rehearsal receipts."""
import fold
from fold_stalls import text


def section(events):
    """Render admitted rehearsal batches, preserving project and candidate identity."""
    batches, removed = {}, {}
    ordered = sorted((e for e in events if 'actor' not in e or
                      fold.admits(e, e['actor']) is None), key=lambda e: fold.ts(e['ts']))
    for event in ordered:
        kind, subject = event.get('type'), event.get('subject')
        project = event.get('project')
        if kind == 'queue-add':
            removed.pop((project, subject), None)
        elif kind == 'queue-remove':
            removed[project, subject] = event
        if kind == 'queue-batch' and project:
            batches[project, subject] = dict(event, receipt=None, terminal=False)
        elif kind == 'window-rehearsed' and (project, subject) in batches:
            batches[project, subject]['receipt'] = event
        elif kind in ('window-verified', 'window-redcut', 'escalation-blocking') or (
                kind == 'window-cancelled' and event.get('reason') == 'escalation-default'):
            matches = [key for key in batches if key[1] == subject and
                       (project is None or key[0] == project)]
            # Legacy terminal receipts have no project. An ambiguous batch ID
            # cannot close either project's observation.
            if len(matches) == 1:
                batches[matches[0]]['terminal'] = True

    rows = []
    for (project, ident), batch in batches.items():
        if fold.PROJECT and project != fold.PROJECT:
            continue
        receipt = batch['receipt']
        if receipt is None and not batch.get('rehearsal'):
            continue
        receipt = receipt or {}
        if not batch['terminal']:
            members = batch.get('specs', '')
            if isinstance(members, list):
                members = ','.join(map(str, members))
            base = receipt.get('base_sha', batch.get('base_sha', 'undetermined'))
            candidate = receipt.get('sha', batch.get('sha', 'undetermined'))
            rows.append(f"  {text(project)} · {text(ident)} · rehearsal={text(receipt.get('state', 'pending'))}"
                        f" · candidate={text(candidate)} · specs={text(members)}"
                        f" · stacked-on={text(base)} · chain={text(base)} -> {text(candidate)}")
        attribution = receipt.get('attribution') or {}
        if not isinstance(attribution, dict):
            rows.append(f"  {text(project)} · {text(ident)} · attribution=undetermined · reason=invalid attribution")
            continue
        for label, field in (('ATTRIBUTED', 'proven'), ('FALLBACK', 'drop')):
            blamed = attribution.get(field, {})
            if not isinstance(blamed, dict):
                rows.append(f"  {text(project)} · {text(ident)} · {label}=undetermined · reason=invalid attribution")
                continue
            for spec, nodes in sorted(blamed.items()):
                correction = removed.get((project, spec), {})
                parked = (correction.get('window') == ident and
                          correction.get('project', project) == project)
                if batch['terminal'] and not parked:
                    continue
                node_text = ','.join(map(str, nodes)) if isinstance(nodes, list) else str(nodes)
                rows.append(f"  {text(project)} · {text(ident)} · {label} {text(spec)}"
                            f" · disposition={'parked' if parked else 'reported'} · nodes={text(node_text)}"
                            + (f" · reason={text(correction['reason'])}" if parked else ''))
    return ['## MERGE LANE', *rows, ''] if rows else []
