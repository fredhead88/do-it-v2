#!/usr/bin/env python3
"""Package repeated dysfunction for Thinker triage; render a short offender list.

The hourly entry point and flags are unchanged. package(events, now) is pure;
only main/persist touch local files or append to the existing ledger.
"""
import argparse
import collections
import datetime as dt
from decimal import Decimal, DecimalException
import fcntl
import hashlib
import html
import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(os.environ.get('DOIT_ROOT', os.path.expanduser('~/.do-it')))
LOG = pathlib.Path('/opt/albert-scott/docs/sessions/do-it-v2-lessons-log.md')
START, END = '<!-- digest:start -->', '<!-- digest:end -->'
UTC = dt.timezone.utc
EXCLUDED = {'unassigned', 'stuck-over-1h'}
RECURRENCE = re.compile(rb'^recurred \d+ times since filed(?:\r?\n|$)', re.M)


def _modules():
    source = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
    if source not in sys.path:
        sys.path.insert(0, source)
    import fold
    import problems
    return fold, problems


def events():
    # Clearing the environment alone is insufficient for already-loaded fold.
    os.environ.pop('DOIT_PROJECT', None)
    fold, _ = _modules()
    fold.PROJECT = None
    return fold.read_events()


def timestamp(value):
    try:
        value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value.astimezone(UTC) if value.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def text(value):
    """Literal Markdown text: no injected rows, HTML, links or formatting."""
    value = ' '.join(str(value).split())
    value = html.escape(value, quote=True)
    return re.sub(r'[\\`*_{}\[\]()#!|:]', lambda m: f'&#{ord(m[0])};', value)


def eligible(evs, now, since=None):
    _, problems = _modules()
    admitted = problems._emits_filter(evs)
    result, seen, omitted = [], set(), collections.Counter()
    for event in admitted:
        if event.get('type') not in ('lesson', 'problem-grouped'):
            omitted['other event types (not dysfunction occurrences)'] += 1
            continue
        when = timestamp(event.get('ts'))
        slug = problems._slug_of(event)
        source = event.get('_src')
        reason = None
        if not isinstance(slug, str) or slug in EXCLUDED:
            reason = 'excluded slug'
        elif event.get('verdict') == 'working':
            reason = 'working verdict'
        elif not isinstance(event.get('subject'), str) or not event['subject'].strip():
            reason = 'missing subject'
        elif when is None:
            reason = 'invalid timestamp'
        elif when > now:
            reason = 'future timestamp'
        elif since is not None and when < since:
            reason = 'outside reporting window'
        elif source and source in seen:
            reason = 'copied source'
        if reason:
            omitted[reason] += 1
            continue
        if source:
            seen.add(source)
        result.append(event)
    result.sort(key=lambda e: (timestamp(e['ts']), str(e.get('_src', ''))))
    return result, omitted


def groups(evs):
    _, problems = _modules()
    out = collections.defaultdict(list)
    for event in evs:
        out[problems._slug_of(event)].append(event)
    return out


def qualifying(evs):
    _, problems = _modules()
    candidates = [(slug, rows) for slug, rows in groups(evs).items()
                  if problems.occurrence_count(rows, slug) >= 3
                  and len({e['subject'] for e in rows}) >= 2]
    return sorted(candidates, key=lambda item: (timestamp(item[1][0]['ts']), item[0]))


def canonical_briefs(evs):
    briefs = {}
    minimum = dt.datetime.min.replace(tzinfo=UTC)
    for event in sorted((e for e in evs if e.get('type') == 'brief'),
                        key=lambda e: (timestamp(e.get('ts')) or minimum, str(e.get('_src', '')))):
        subject = event.get('subject')
        if isinstance(subject, str) and subject.startswith('problem:'):
            briefs.setdefault(subject, event)
    return briefs


def costs(rows):
    labels = []
    for field, unit in (('cost_min', 'min'), ('cost_tokens', 'tokens')):
        total, missing = Decimal(0), 0
        for event in rows:
            try:
                value = event.get(field)
                if isinstance(value, bool):
                    raise ValueError
                number = Decimal(str(value))
                if not number.is_finite() or number < 0:
                    raise ValueError
                total += number
            except (DecimalException, ValueError):
                missing += 1
        labels.append(f'{total:g} {unit} measured ({missing} unmeasured)')
    return '; '.join(labels)


def brief_body(slug, rows):
    description = next((e.get('statement') or e.get('text') for e in rows
                        if e.get('statement') or e.get('text')), 'Repeated dysfunction; description unknown.')
    description = re.split(r'(?<=[.!?])\s+', ' '.join(str(description).split()), maxsplit=1)[0]
    refs = []
    for event in rows:
        ref = event.get('ref')
        ref = ref.strip() if isinstance(ref, str) else ''
        if not ref or ref in refs:
            ref = event.get('_src')
        if isinstance(ref, str) and ref.strip() and ref not in refs:
            refs.append(ref)
        if len(refs) == 5:
            break
    workarounds = list(dict.fromkeys(str(e['workaround']) for e in rows if e.get('workaround')))
    step = next((e['step'] for e in rows if e.get('step')), 'unknown')
    file = next((e['file'] for e in rows if e.get('file')), 'unknown')
    lines = [f'# Problem: {text(slug)}', '', text(description), '',
             f'Step: {text(step)}; file: {text(file)}.', '', 'Evidence:']
    lines += ['- ref: ' + text(ref) for ref in refs]
    if len(refs) < 3:
        lines.append(f'Only {len(refs)} distinct evidence refs recorded; missing refs unknown.')
    lines += ['', 'Workarounds: ' + ('; '.join(text(w) for w in workarounds) or 'none recorded'),
              'Total cost so far: ' + costs(rows), '']
    return '\n'.join(lines)


def package(evs, now):
    """Deterministically select creations/recurrences, independent of env and IO."""
    _, problems = _modules()
    history, _ = eligible(evs, now)
    recent, _ = eligible(evs, now, now - dt.timedelta(days=14))
    by_slug = groups(history)
    briefs = canonical_briefs(evs)
    spent = sum(1 for e in evs if e.get('type') == 'brief' and e.get('actor') == 'digest'
                and isinstance(e.get('subject'), str) and e['subject'].startswith('problem:')
                and timestamp(e.get('ts')) is not None and timestamp(e['ts']).date() == now.date())
    operations = []
    for subject, event in sorted(briefs.items()):
        filed = timestamp(event.get('ts'))
        members = by_slug.get(subject[8:], [])
        if members and filed is None:
            raise ValueError('canonical brief has invalid filing timestamp')
        later = [e for e in members if timestamp(e['ts']) > filed]
        count = problems.occurrence_count(later, subject[8:])
        if count:
            operations.append(dict(kind='recurrence', subject=subject, path=event.get('path'),
                                   line=f'recurred {count} times since filed'))
    for slug, _ in qualifying(recent):
        subject = 'problem:' + slug
        if subject in briefs or spent >= 2:
            continue
        operations.append(dict(kind='brief', subject=subject, text=brief_body(slug, by_slug[slug])))
        spent += 1
    return operations


def content_path(raw):
    """Resolve an existing canonical path beneath resolved ROOT/content."""
    if not isinstance(raw, str) or not raw:
        raise ValueError('canonical brief content path missing')
    candidate = pathlib.Path(raw)
    if '..' in candidate.parts:
        raise ValueError('unsafe canonical brief content path')
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    target = candidate.resolve(strict=True)
    content = (ROOT / 'content').resolve(strict=True)
    if not target.is_relative_to(content) or not target.is_file():
        raise ValueError('canonical brief content is not a contained regular file')
    return target


def persist(operations):
    """Apply under the caller's digest lock; content always precedes its event."""
    fold, _ = _modules()
    for operation in operations:
        if operation['kind'] == 'recurrence':
            path = content_path(operation.get('path'))
            before = path.read_bytes()
            line = operation['line'].encode() + b'\n'
            matches = list(RECURRENCE.finditer(before))
            if matches:
                after = RECURRENCE.sub(lambda match: line if match.start() == matches[0].start() else b'', before)
            else:
                after = before + (b'' if not before or before.endswith(b'\n') else b'\n') + line
            if before != after:
                # Atomic replacement prevents a crash from truncating canonical content.
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.digest-', delete=False) as stream:
                    stream.write(after)
                    stream.flush()
                    os.fsync(stream.fileno())
                    temporary = pathlib.Path(stream.name)
                temporary.chmod(path.stat().st_mode & 0o777)
                os.replace(temporary, path)
        else:
            folder = ROOT / 'content'
            folder.mkdir(parents=True, exist_ok=True)
            body = operation['text'].encode()
            key = hashlib.sha256(operation['subject'].encode()).hexdigest()[:32]
            path = folder / f'L-brief-digest-{fold.NOW.date()}-{key}.md'
            try:
                with path.open('xb') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                # Reuse only our identical orphan; never overwrite unrelated content or follow symlinks.
                if path.is_symlink() or path.read_bytes() != body:
                    with tempfile.NamedTemporaryFile(dir=folder, prefix=f'L-brief-digest-{key}-',
                                                     suffix='.md', delete=False) as stream:
                        stream.write(body)
                        stream.flush()
                        os.fsync(stream.fileno())
                        path = pathlib.Path(stream.name)
            previous = os.environ.get('DOIT_LEDGER_FILE')
            os.environ['DOIT_LEDGER_FILE'] = 'L-digest-local.jsonl'
            try:
                fold.append(['brief', operation['subject'], 'project=do-it-v2', f'path={path.resolve()}'])
            finally:
                if previous is None:
                    os.environ.pop('DOIT_LEDGER_FILE', None)
                else:
                    os.environ['DOIT_LEDGER_FILE'] = previous


def render(evs, days, now=None):
    now = now or dt.datetime.now(UTC)
    recent, omitted = eligible(evs, now, now - dt.timedelta(days=days))
    briefs = canonical_briefs(evs)
    _, problems = _modules()
    lines = [START, f'_Repeat offenders over the last {days} days; generated {now.isoformat(timespec="minutes")}._',
             '', '| problem | occurrences | subjects | measured cost | brief |', '|---|---:|---:|---|---|']
    candidates = qualifying(recent)
    for slug, members in candidates:
        event = briefs.get('problem:' + slug)
        state = 'pending-cap'
        if event:
            target = content_path(event.get('path'))
            state = '[brief](' + target.as_uri() + ')'
        lines.append(f'| {text(slug)} | {problems.occurrence_count(members, slug)} | '
                     f'{len({e["subject"] for e in members})} | {costs(members)} | {state} |')
    singletons = sum(len(members) for slug, members in groups(recent).items()
                     if slug not in {slug for slug, _ in candidates})
    omitted['below repeat threshold'] += singletons
    summary = '; '.join(f'{reason}: {count}' for reason, count in sorted(omitted.items()) if count)
    lines += ['', 'Omitted ledger rows: ' + (summary or '0') + '.', END]
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log', default=str(LOG))
    parser.add_argument('--days', type=int, default=14)
    args = parser.parse_args()
    try:
        if args.days < 0:
            raise ValueError('reporting days must be nonnegative')
        ROOT.mkdir(parents=True, exist_ok=True)
        with (ROOT / '.lessons-digest.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            evs = events()
            now = dt.datetime.now(UTC)
            fold, _ = _modules()
            fold.NOW = now
            persist(package(evs, now))
            new = render(events(), args.days, now)
            log = pathlib.Path(args.log)
            body = log.read_bytes().decode('utf-8') if log.exists() else ''
            if START in body and END in body and body.index(START) < body.index(END):
                body = body[:body.index(START)] + new + body[body.index(END) + len(END):]
            else:
                body += '\n\n## Generated repeat offenders\n\n' + new + '\n'
            log.write_bytes(body.encode('utf-8'))
        print(f'digest written ({len(new.splitlines())} generated lines)')
        return 0
    except (OSError, ValueError, TypeError):
        # Never dump arbitrary ledger text, body contents, or untrusted paths.
        print('lessons-digest: packaging failed; check canonical content and local write access', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
