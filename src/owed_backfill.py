#!/usr/bin/env python3
"""Plan an audited owed backlog repair; only --apply appends events."""
import argparse
from collections import Counter, defaultdict
from datetime import timedelta
import json
import os
import pathlib
import sys
import tempfile

import fold
import owed
import owed_digest
import owed_routes

OPEN = frozenset(('waiting', 'due', 'blocked', 'failed', 'correcting', 'quarantined', 'expired'))
CORRECTIVES = {
    ('L-spec-0152', 'AC4'): 'L-spec-0998',
    ('L-spec-0268', 'AC7'): 'L-spec-0998',
    ('L-spec-0298', 'AC6'): 'L-spec-0998',
    ('L-spec-0661', 'AC4'): 'L-spec-0998',
    ('L-spec-0667', 'AC7'): 'L-spec-0999',
    ('L-spec-0895', 'AC7'): 'L-spec-0999',
    ('L-spec-0396', 'AC10'): 'L-spec-1001',
    ('L-spec-0633', 'AC12'): 'L-spec-1001',
    ('L-spec-0916', 'AC4'): 'L-spec-1001',
}


def target_capability(row):
    if 'pg_read_all_stats' in str(row.get('ask') or '').lower():
        return 'operator-action'
    return owed_routes.classify_ask(row.get('ask'), row.get('capability'))['capability']


def _rows(evs_by_spec, now):
    return [row for sid in sorted(evs_by_spec) for row in owed.checks(evs_by_spec[sid], now)]


def plan(evs_by_spec, audit_rows, now):
    """Pure actions, including every skipped audit outcome and open relabel row."""
    rows = _rows(evs_by_spec, now)
    indexed = {(r['spec'], r['criterion']): r for r in rows}
    actions, failures_seen, correctives_seen = [], set(), set()

    def add(section, sid, cid, reason, event=None, **fields):
        actions.append(dict(section=section, spec=sid, criterion=cid, reason=reason, event=event, **fields))

    for audit in audit_rows:
        sid, cid = audit['spec'], audit['criterion']
        key = sid, cid
        row = indexed.get(key)
        status = row['status'] if row else 'absent'
        fields = dict(status=status, capability=row.get('capability') if row else None,
                      audit_class=audit['class'], verdict=audit['verdict'])
        if audit['class'] == 'CLOSE' or audit['verdict'] == 'CLOSE':
            add('audit', sid, cid, 'CLOSE already waived' if status == 'waived' else 'CLOSE not waived; operator owns waiver', **fields)
            continue
        if audit['class'] != 'CRITICAL' or audit['verdict'] != 'FAIL':
            if audit['class'] == 'CRITICAL' and audit['verdict'] == 'COULD-NOT-RUN':
                add('audit', sid, cid, 'COULD-NOT-RUN; waiting on routes', **fields)
            else:
                add('skipped-class', sid, cid, 'audit reading is not the check observation; sweep and failure-escalation own outcomes', **fields)
            continue
        reason, failure = None, None
        if row is None:
            reason = 'no open row'
        elif status not in OPEN:
            reason = f'not open: {status}'
        elif status in ('failed', 'correcting') or row['last_failed_at'] is not None or key in failures_seen:
            reason = 'skipped-already-present: owed-failed'
        else:
            command, evidence = str(audit.get('how_to_check') or '').strip(), str(audit.get('evidence') or '').strip()
            # Empty observations remain invalid REQUIRED fields, not fake output.
            proof = '$ ' + command + '\n' + evidence if command and evidence else ''
            failure = dict(type='owed-failed', subject=sid, criterion=cid, kind='unmet', evidence=proof)
            reason = 'CRITICAL FAIL observed by audit'
            failures_seen.add(key)
        add('audit', sid, cid, reason, failure, event_type='owed-failed', **fields)
        corrective = CORRECTIVES.get(key)
        if corrective is None:
            continue
        history = evs_by_spec.get(sid, [])
        existing = any(e.get('type') == 'owed-corrective' and e.get('criterion') == cid
                       and e.get('corrective') == corrective for e in history)
        correction = None
        if existing or key in correctives_seen:
            reason = 'skipped-already-present: owed-corrective ' + corrective
        elif row is None or status not in OPEN or (failure is None and row['last_failed_at'] is None):
            reason = 'no open failed row for corrective ' + corrective
        else:
            latest = max(now, row['last_failed_at'] or now)
            correction = dict(type='owed-corrective', subject=sid, criterion=cid, corrective=corrective,
                              reason='linked by topic to corrective spec ' + corrective,
                              ts=(latest + timedelta(seconds=1)).isoformat(timespec='seconds'))
            reason = correction['reason']
            correctives_seen.add(key)
        add('audit', sid, cid, reason, correction, event_type='owed-corrective', **fields)

    for row in rows:
        if row['status'] not in OPEN:
            continue
        target, current = target_capability(row), row.get('capability')
        event = None
        if target == current:
            reason = 'skipped-already-present: capability already right'
        elif current not in ('other', None, ''):
            reason = 'not legacy'
        elif not str(row.get('ask') or '').strip():
            reason = 'no ask, not relabelled'
        elif target not in owed_routes.ROUTES:
            reason = 'target has no route'
        else:
            reason = 'legacy capability with ask; target differs'
            event = dict(type='owed-routed', subject=row['spec'], criterion=row['criterion'], route='relabel', capability=target)
        add('relabel', row['spec'], row['criterion'], reason, event, event_type='owed-routed', status=row['status'], capability=current, target=target)
    return actions


def _readable_dir(path):
    if (not path.is_dir() or not path.stat().st_mode & 0o444 or not path.stat().st_mode & 0o111
            or not os.access(path, os.R_OK | os.X_OK)):
        raise OSError('directory unavailable: ' + str(path))


def _local_json(path, directory):
    if not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError('JSON path leaves given directory: ' + str(path))
    return json.loads(path.read_text())


def _audits(directory):
    _readable_dir(directory)
    rows = []
    for path in sorted(directory.glob('result-*.json')):
        data = _local_json(path, directory)
        if not isinstance(data, list):
            raise ValueError('audit JSON must be a list: ' + str(path))
        for row in data:
            if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip()
                                                for k in ('spec', 'criterion', 'class', 'verdict')):
                raise ValueError('invalid audit row: ' + str(path))
        rows.extend(data)
    return rows


def _labels(path):
    if not path.exists():
        return {}, 'unavailable'
    data = _local_json(path, path.parent)
    if not isinstance(data, dict):
        raise ValueError('labels must be a key-to-capability object')
    if any(str(key).isdigit() for key in data):
        source = _local_json(path.parent / 'unobs_open.json', path.parent)
        mapped = {}
        for key, value in data.items():
            row = source[int(key)]
            mapped[f"{row['spec']}/{row['crit']}"] = value
        data = mapped
    if not all(isinstance(k, str) and '/' in k and isinstance(v, str) for k, v in data.items()):
        raise ValueError('invalid labels')
    return data, 'available'


def _admission(events):
    classes, items, prior = Counter(), [], defaultdict(list)
    for event in events:
        actor, kind, sid = event.get('actor', ''), event.get('type', ''), event.get('subject', '')
        reason = fold.admits(event, actor, prior[sid])
        if reason:
            classes[(kind, actor)] += 1
        else:
            prior[sid].append(event)
        if kind in ('owed-met', 'owed-failed', 'owed-waived'):
            items.append((event, reason))
    return classes, items


def _counts(rows):
    opened = [r for r in rows if r['status'] in OPEN]
    return len(opened), Counter(r['status'] for r in opened), Counter(r.get('capability') or 'missing' for r in opened)


def _span(value):
    return owed_digest._span(value, 300)


def render(before, after, events, actions, retired, labels_state, written):
    lines = ['# Owed backfill report', '', '## Before']
    for title, rows in (('before', before), ('after', after)):
        if title == 'after':
            lines += ['', '## After']
        if rows is None:
            lines.append('after: not applied')
            continue
        total, statuses, causes = _counts(rows)
        lines.append(f'open total: {total}')
        excluded = Counter(r['status'] for r in rows if r['status'] not in OPEN)
        lines += [f'non-open excluded from backfill / {status}: {count}' for status, count in sorted(excluded.items())]
        lines += [f'status {name}: {statuses[name]}' for name in sorted(OPEN)]
        lines += [f'cause {_span(name)}: {count}' for name, count in sorted(causes.items())]
        lines += [f"{r['spec']}/{r['criterion']} status={r['status']}" for r in rows if r['status'] in OPEN]
    uv_before = sum(r['status'] == 'unverifiable' for r in before)
    uv_after = sum(r['status'] == 'unverifiable' for r in after) if after is not None else 'not applied'
    lines += ['', '## Unverifiable', f'unverifiable closes: before={uv_before} after={uv_after}', '', '## Admission']
    classes, items = _admission(events)
    lines += [f'refused total: {sum(classes.values())}; class sum: {sum(classes.values())}']
    lines += [f'{kind} / {actor}: {count}' for (kind, actor), count in sorted(classes.items())]
    for event, reason in items:
        lines.append(' / '.join(_span(event.get(k)) for k in ('subject', 'criterion', 'ts', 'actor'))
                     + (' still-dropped: ' + _span(reason) if reason else ' admitted'))
    pending = sum(a.get('event') is not None for a in actions)
    lines += ['', '## Audit outcomes', f'pending writes: {pending}', f'written: {written}',
              f"skipped-already-present: {sum(a['reason'].startswith('skipped-already-present') for a in actions)}"]
    outcomes = Counter(a['reason'] for a in actions if a['section'] == 'audit' and not a.get('event'))
    lines += [f'skipped {_span(reason)}: {count}' for reason, count in sorted(outcomes.items())]
    lines += [f"CLOSE already waived: {sum(a['reason'] == 'CLOSE already waived' for a in actions)}",
              f"CLOSE not waived: {sum(a['reason'].startswith('CLOSE not waived') for a in actions)}"]
    for action in actions:
        if action['section'] == 'audit':
            lines.append(_action_line(action))
    lines += ['', '## Verify reconciliation']
    verify = {f"{r['spec']}/{r['criterion']}": r for r in before if r['status'] in OPEN and r['criterion'] == 'verify'}
    lines.append('retired-orphans: ' + ('unavailable' if retired is None else 'available'))
    retired_set = set(retired or [])
    union = set(verify) | retired_set
    lines += ['| key | in-ledger-open | status | route or quarantine reason |', '| --- | --- | --- | --- |']
    for key in sorted(union):
        row = verify.get(key)
        lines.append(f"| {_span(key)} | {'yes' if row else 'no'} | {_span(row['status'] if row else 'retired')} | "
                     + _span((row.get('quarantine_reason') or row.get('capability')) if row else 'retired orphan') + ' |')
    lines += [f'{len(union)} vs 92: {len(union)-92:+d}',
              'only open: ' + ', '.join(sorted(set(verify) - retired_set)),
              'only retired: ' + ', '.join(sorted(retired_set - set(verify)))]
    if retired is None:
        lines.append('union and source differences provisional: retired source unavailable')
    lines += ['', '## Relabels', 'probe labels: ' + labels_state]
    relabel_skips = Counter(a['reason'] for a in actions if a['section'] == 'relabel' and not a.get('event'))
    lines += [f'skipped {_span(reason)}: {count}' for reason, count in sorted(relabel_skips.items())]
    for action in actions:
        if action['section'] == 'relabel':
            lines.append(_action_line(action))
    for title, predicate in (('no ask, not relabelled', lambda a: a['reason'] == 'no ask, not relabelled'),
                             ('probe disagreements', lambda a: a['reason'].startswith('probe disagreement'))):
        lines.append('### ' + title)
        lines += [_action_line(a) for a in actions if a['section'] == 'relabel' and predicate(a)]
    lines += ['', '## Skipped audit classes']
    skipped = Counter()
    for action in actions:
        if action['section'] == 'skipped-class':
            cls, verdict = action['audit_class'], action['verdict']
            label = cls if cls == verdict else cls + ' ' + verdict
            skipped[label] += 1
    for label in ('CRITICAL PASS', 'WORTH-A-LOOK FAIL', 'WORTH-A-LOOK', 'WORTH-A-LOOK COULD-NOT-RUN'):
        lines.append(f'{label}: {skipped.pop(label, 0)}; audit is not check observation; sweep/failure-escalation own outcomes')
    lines += [f'{_span(label)}: {count}; outside backfill writes' for label, count in sorted(skipped.items())]
    return '\n'.join(lines) + '\n'


def _action_line(action):
    event = action.get('event')
    kind = event['type'] if event else action.get('event_type', 'no-event')
    disposition = 'would-be-refused: ' + action['refusal'] if action.get('refusal') else ('written' if action.get('written') else 'planned' if event else 'skipped')
    detail = json.dumps(event, ensure_ascii=False, sort_keys=True) if event else ''
    return (f"{_span(action['spec'])}/{_span(action['criterion'])} {kind} {disposition} "
            f"{action.get('verdict', '')} status={action.get('status')} capability={action.get('capability')} "
            f"reason={_span(action['reason'])} {_span(detail)}")


def _write_report(text):
    path = fold.ROOT / 'content/L-owed-backfill-report.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '.', delete=False) as output:
            temporary = pathlib.Path(output.name)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--audit', type=pathlib.Path, default=fold.ROOT / 'content/owed-audit')
    parser.add_argument('--labels', type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        _readable_dir(fold.EVENTS)
        events = fold.read_events(use_cache=False)
        audits = _audits(args.audit)
        label_path = args.labels or fold.ROOT / 'content/probe-L-charter-0049/q5_labels.json'
        if args.labels and not label_path.exists():
            raise OSError('labels unavailable: ' + str(label_path))
        labels, labels_state = _labels(label_path)
        retired_path = fold.ROOT / 'state/sweep-owed-orphans.retired.json'
        retired = _local_json(retired_path, retired_path.parent) if retired_path.exists() else None
        if retired is not None and (not isinstance(retired, list) or not all(isinstance(k, str) and '/' in k for k in retired)):
            raise ValueError('invalid retired-orphan keys')
        specs = {sid: spec['evs'] for sid, spec in fold.fold(events)[0].items()}
        before = _rows(specs, fold.NOW)
        actions = plan(specs, audits, fold.NOW)
        actor = fold.actor_of(os.environ.get('DOIT_LEDGER_FILE', 'L-operator-local.jsonl'))
        for action in actions:
            key = f"{action['spec']}/{action['criterion']}"
            if action['section'] == 'relabel' and key in labels and labels[key] != action['target']:
                action.update(event=None, reason=f"probe disagreement: probe={labels[key]} target={action['target']}; not applied")
            if action.get('event'):
                action['refusal'] = fold.check_append(action['event'], actor)
        text = render(before, None, events, actions, retired, labels_state, 0)
        if not args.apply:
            print(text, end='')
            return 0
        refused = [a for a in actions if a.get('refusal')]
        if refused:
            print(text, end='')
            for action in refused:
                print(f"refused: {action['event']['type']} by {actor} subject={action['spec']}: {action['refusal']}", file=sys.stderr)
            return 1
        written = 0
        for action in actions:
            event = action.get('event')
            if event is None:
                continue
            argv = [event['type'], event['subject']] + [k + ':=' + json.dumps(v, ensure_ascii=False)
                                                       for k, v in event.items() if k not in ('type', 'subject')]
            fold.append(argv, strict=True)
            action['written'] = True
            written += 1
        fresh = fold.read_events(use_cache=False)
        after_specs = {sid: spec['evs'] for sid, spec in fold.fold(fresh)[0].items()}
        text = render(before, _rows(after_specs, fold.NOW), events, actions, retired, labels_state, written)
        _write_report(text)
        print(text, end='')
        return 0
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        print('owed-backfill: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
