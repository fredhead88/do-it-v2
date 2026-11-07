"""Durable merge facts; the formatter and named sender own presentation/transport."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid
from zoneinfo import ZoneInfo

import merge_loop_state
import notification_policy
import tg_say

ISRAEL = ZoneInfo('Asia/Jerusalem')


def instant(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('instant needs a timezone')
    return value


def root_path(root=None):
    return Path(root if root is not None else os.environ.get('DOIT_ROOT', Path.home() / '.do-it'))


def identifiers(text):
    tokens = re.split(r'[,\s]+', text.strip())
    if not tokens or any(not re.fullmatch(r'(?:L-spec-)?[0-9]+', t) for t in tokens):
        raise ValueError('invalid spec list')
    return list(dict.fromkeys('L-spec-' + str(int(t.removeprefix('L-spec-'))).zfill(4) for t in tokens))


def classify(text):
    """Only the seven anchored loop templates can confer attribution."""
    value = text.removeprefix('Merge push: ')
    base = dict(kind='unknown', specs=[], context_specs=[], window=None,
                reason='unrecognized input', text=text, disposition='diagnostic')
    def row(kind, specs=(), context=(), window=None, reason=None):
        return base | dict(kind=kind, specs=list(specs), context_specs=list(context),
                           window=window, reason=reason,
                           disposition='pending' if kind in ('merged', 'failed') else 'diagnostic')
    try:
        m = re.fullmatch(r'local rehearsal passed for (.+); the full CI rehearsal has been running since the batch started\. The window reuses it and lands in minutes\.', value)
        if m:
            return [row('progress', context=identifiers(m[1]))]
        m = re.fullmatch(r'rehearsal of (.+?) could not run its tests \((.*)\)\. Not blaming any spec; Thinker is checking\.', value, re.S)
        if m:
            return [row('failed', context=identifiers(m[1]), reason='tests unavailable: ' + m[2])]
        m = re.fullmatch(r'rehearsal of (.+?) found a problem\. Set aside: (.*?)\. Picking replacements and rehearsing again\.', value, re.S)
        if m:
            context = identifiers(m[1])
            specs = identifiers(m[2]) if m[2].strip(' .') else []
            if not set(specs) <= set(context):
                raise ValueError('set aside is outside batch context')
            return [row('failed', specs, context, reason='rehearsal found a problem')]
        m = re.fullmatch(r'window (\S+) merged\. shipped=(.*)', value, re.S)
        if m:
            shipped, delimiter, _ = m[2].partition(' dropped=')
            diagnostics = []
            if not delimiter:
                shipped = shipped.rpartition(',')[0]
                diagnostics = [base | dict(window=m[1], reason='unconfirmed shipped suffix')]
            specs = []
            for token in re.split(r'[,\s]+', shipped.strip()):
                if not token:
                    continue
                try:
                    specs.extend(identifiers(token))
                except ValueError:
                    diagnostics.append(base | dict(window=m[1], reason='malformed shipped token'))
            facts = [row('merged', list(dict.fromkeys(specs)), window=m[1])] if specs else []
            return facts + diagnostics or [base | dict(reason='no confirmed shipped IDs')]
        m = re.fullmatch(r'window (\S+) did not merge: (.*)', value, re.S)
        if m:
            return [row('failed', window=m[1], reason='window did not merge: ' + m[2])]
        m = re.fullmatch(r'full CI rehearsal of (.+?) was not green: (.*)\. Holding that batch; Thinker is checking\.', value, re.S)
        if m:
            return [row('failed', context=identifiers(m[1]), reason='full CI not green: ' + m[2])]
        m = re.fullmatch(r'window (\S+) started: (.+?)\. CI takes about 40 minutes\.', value)
        if m:
            return [row('progress', context=identifiers(m[2]), window=m[1])]
    except ValueError as exc:
        return [base | dict(reason=str(exc))]
    return [base]


@contextmanager
def locked(root):
    directory = root / 'state'
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise OSError('unsafe state directory')
    fd = os.open(directory / 'merge-news.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('unsafe lock file')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read(root):
    path = root / 'state/merge-news.jsonl'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    with os.fdopen(fd) as inp:
        if not stat.S_ISREG(os.fstat(inp.fileno()).st_mode):
            raise OSError('unsafe store file')
        rows = [json.loads(line) for line in inp]
    seen = set()
    for r in rows:
        if (not isinstance(r, dict) or not isinstance(r.get('id'), str) or r['id'] in seen
                or r.get('kind') not in ('merged', 'failed', 'progress', 'unknown')
                or r.get('disposition') not in ('pending', 'sent', 'daily', 'diagnostic')
                or not isinstance(r.get('text'), str)):
            raise ValueError('corrupt merge-news store')
        seen.add(r['id'])
        instant(r['time'])
        for name in ('specs', 'context_specs'):
            if not isinstance(r.get(name), list) or any(not isinstance(s, str) or not re.fullmatch(r'L-spec-[0-9]{4,}', s) for s in r[name]):
                raise ValueError('corrupt stored spec list')
        for name in ('window', 'reason'):
            if r.get(name) is not None and not isinstance(r[name], str):
                raise ValueError('corrupt stored field')
        if r['disposition'] == 'sent':
            instant(r['sent_at'])
    return rows


def write(root, rows):
    path = root / 'state/merge-news.jsonl'
    scratch = path.with_name('merge-news.' + uuid.uuid4().hex)
    try:
        fd = os.open(scratch, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as out:
            for row in rows:
                out.write(json.dumps(row, ensure_ascii=False) + '\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(scratch, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        scratch.unlink(missing_ok=True)


def identity(row):
    # Context is identity for unblamed batch failures; it never becomes blame.
    return (tuple(row['specs']), ' '.join((row['reason'] or '').split()),
            () if row['specs'] else tuple(row['context_specs']),
            None if row['specs'] else row['window'])


def acknowledged(rows, now):
    day = now.astimezone(ISRAEL).date()
    return {identity(r) for r in rows if r['kind'] == 'failed' and r['disposition'] == 'sent'
            and instant(r['sent_at']).astimezone(ISRAEL).date() == day}


def record(text, root=None, now=None):
    root = root_path(root)
    now = instant(now or datetime.now(ISRAEL))
    with locked(root):
        rows = read(root)
        sent = acknowledged(rows, now)
        added = []
        for fact in classify(text):
            # Individual attributed facts permit partial suppression/overflow.
            for specs in ([[s] for s in fact['specs']] or [[]]):
                row = fact | dict(specs=specs, id=uuid.uuid4().hex, time=now.isoformat())
                if row['kind'] == 'failed' and identity(row) in sent:
                    row.update(disposition='daily', disposition_reason='already named today')
                added.append(row)
        write(root, rows + added)
        return added


def budget(root, now):
    ids = set()
    with (root / 'logs/tg-send.jsonl').open() as inp:
        for line in inp:
            r = json.loads(line)
            if not isinstance(r, dict):
                raise ValueError('corrupt sender log')
            day = instant(r['time']).astimezone(ISRAEL).date()
            if r.get('event') == 'result' and r.get('http') == 200 and r.get('class') != 'reply' and day == now.astimezone(ISRAEL).date():
                if not isinstance(r.get('id'), str) or not r['id']:
                    raise ValueError('missing sender identity')
                ids.add(r['id'])
    return len(ids)


def fact_line(row, root):
    if row['specs']:
        verb = 'Merged' if row['kind'] == 'merged' else 'Rehearsal failed for'
        return verb + ' ' + '; '.join(s + ': ' + merge_loop_state.plain_line(s, root=root) for s in row['specs'])
    if row['window']:
        # Window names are untrusted diagnostic identifiers, not prose.
        return 'A merge window failed; no individual change is blamed.'
    if row['reason'].startswith('tests unavailable:'):
        return 'Batch tests could not run; no individual change is blamed.'
    if row['reason'].startswith('full CI not green:'):
        return 'The full CI rehearsal failed; the batch is held without individual blame.'
    return 'A batch rehearsal failed; no individual change is blamed.'


def formatted(rows, root, now):
    release = merge_loop_state.next_release(now, root=root)
    by = 'No release is scheduled.' if release is None else 'Next release expected: ' + release + ' Israel time.'
    lines = []
    def render():
        more = len(rows) - len(lines)
        body = '\n'.join(lines + ([f'and {more} more'] if more else []))
        return notification_policy.format_message('[MERGED]', 'Merge news',
                    'Confirmed merge and rehearsal results:\n' + body, 'nothing.', by, None)
    for row in rows:
        lines.append(fact_line(row, root))
        if len(render()) > 3500:
            lines.pop()
            break
    text = render()
    if len(text) > 3500:
        raise ValueError('release statement exceeds message limit')
    return text, len(lines)


def _flush(root, now):
    with locked(root):
        rows = read(root)
        pending = [r for r in rows if r['disposition'] == 'pending']
        if not pending:
            return dict(state='idle')
        overnight = any(tg_say.quiet(instant(r['time'])) and instant(r['time']) <= now for r in pending)
        oldest = min(instant(r['time']) for r in pending)
        if now - oldest < timedelta(minutes=20) and not overnight:
            return dict(state='waiting')
        sent = acknowledged(rows, now)
        eligible = []
        duplicates = []
        seen = set()
        for r in pending:
            key = identity(r)
            if r['kind'] == 'failed' and key in sent:
                r.update(disposition='daily', disposition_reason='already named today')
            elif r['kind'] == 'failed' and key in seen:
                duplicates.append(r)
            else:
                eligible.append(r)
                if r['kind'] == 'failed':
                    seen.add(key)
        try:
            count = budget(root, now)
            budget_reason = 'daily message budget reached' if count >= 15 else None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            budget_reason = 'budget-unread'
            rows.append(dict(id=uuid.uuid4().hex, time=now.isoformat(), kind='unknown', specs=[],
                             context_specs=[], window=None, reason='budget-unread', text='', disposition='diagnostic'))
        if budget_reason and not any(r['kind'] == 'merged' for r in eligible):
            for r in eligible + duplicates:
                r.update(disposition='daily', disposition_reason=budget_reason)
            write(root, rows)
            return dict(state='daily', reason=budget_reason)
        if not eligible:
            write(root, rows)
            return dict(state='idle')
        text, kept = formatted(eligible, root, now)
        result = tg_say.deliver('merge-news', text, label='[MERGED]', root=root)
        if result.state != 'sent':
            raise RuntimeError('merge-news delivery ' + result.state)
        named = {identity(r) for r in eligible[:kept] if r['kind'] == 'failed'}
        for r in eligible[:kept]:
            r.update(disposition='sent', sent_at=now.isoformat())
        for r in eligible[kept:]:
            r.update(disposition='daily', disposition_reason='overflow: represented only in omitted count')
        for r in duplicates:
            if identity(r) in named:
                r.update(disposition='daily', disposition_reason='already named today')
            else:
                r.update(disposition='daily', disposition_reason='overflow: represented only in omitted count')
        write(root, rows)
        return dict(state='sent', named=kept, omitted=len(eligible)-kept)


def flush(root=None, now=None):
    root = root_path(root)
    now = instant(now or datetime.now(ISRAEL))
    if tg_say.quiet(now):
        return dict(state='quiet')
    error = None
    try:
        result = _flush(root, now)
    except Exception as exc:
        error = exc
    try:
        deliveries = tg_say.drain_held(now, root=root)
        if any(d.state != 'sent' for d in deliveries):
            raise RuntimeError('held drain failed')
    except Exception as exc:
        error = error or exc
    if error:
        raise RuntimeError('merge-news flush failed; pending facts retained') from error
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('record').add_argument('text')
    sub.add_parser('flush')
    args = parser.parse_args(argv)
    try:
        result = record(args.text) if args.command == 'record' else flush()
        print(json.dumps(dict(recorded=len(result)) if args.command == 'record' else result))
        return 0
    except Exception:
        # Diagnostics must not echo an untrusted record or private exception data.
        print('merge-news could not persist or deliver facts; inspect local state', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
