"""Named Telegram delivery, private audit metadata and persistent overnight holds."""
import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid
from zoneinfo import ZoneInfo

from notification_policy import LABELS, format_message, message_label, validate_message

ISRAEL = ZoneInfo('Asia/Jerusalem')
SENDER = Path(__file__).resolve().parents[1] / 'scripts/tg-send.sh'


@dataclass(frozen=True)
class Delivery:
    state: str
    http: int | None = None


def _root(root=None):
    return Path(root if root is not None else os.environ.get('DOIT_ROOT', Path.home() / '.do-it'))


def _now():
    return datetime.now(ISRAEL)


def quiet(now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('instant needs a timezone')
    return now.astimezone(ISRAEL).hour >= 23 or now.astimezone(ISRAEL).hour < 7


@contextmanager
def _private(path, mode='a+'):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise OSError('unsafe state directory')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK |
                 (os.O_APPEND if mode == 'a+' else 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('unsafe state file')
        os.fchmod(fd, 0o600)
        out = os.fdopen(fd, mode)
    except BaseException:
        os.close(fd)
        raise
    with out:
        fcntl.flock(out, fcntl.LOCK_EX)
        yield out


def _append(path, row):
    with _private(path) as out:
        out.write(json.dumps(row, ensure_ascii=False) + '\n')
        out.flush()
        os.fsync(out.fileno())


def _sender(sender):
    if not isinstance(sender, str) or not sender.strip() or len(sender) > 120 or any(ord(c) < 32 for c in sender):
        raise ValueError('invalid sender')


def _audit(root, sender, text, phase, ident, http=None, reason=None, kind=None):
    try:
        label = message_label(text)
    except ValueError:
        label = None
    row = dict(time=_now().isoformat(timespec='seconds'), sender=sender, label=label,
               **{'class': kind or os.environ.get('TG_CLASS', 'automated')}, length=len(text),
               http=http, id=ident, event=phase)
    if row['class'] == 'reply':
        row['reply_to'] = os.environ.get('TG_REPLY_TO')
    if reason:
        row['reason'] = reason
    _append(_root(root) / 'logs/tg-send.jsonl', row)


def _prepare(sender, text, document, mode):
    ident = uuid.uuid4().hex
    reason = None
    try:
        _sender(sender)
        kind = os.environ.get('TG_CLASS', 'automated')
        if kind == 'reply':
            if not re.fullmatch(r'[0-9]+', os.environ.get('TG_REPLY_TO', '')) or int(os.environ['TG_REPLY_TO']) <= 0:
                raise ValueError('invalid reply id')
        elif kind == 'automated':
            validate_message(text)
        else:
            raise ValueError('invalid message class')
        if mode == 'document' and (not document or not Path(document).is_file()):
            raise ValueError('missing document')
    except ValueError as exc:
        reason = str(exc)
    _audit(None, sender, text, 'refused' if reason else 'attempt', ident, reason=reason)
    if reason:
        return 2
    print(ident)
    return 0


def _transport(sender, text, root):
    env = dict(os.environ, DOIT_ROOT=str(root), TG_SENDER=sender)
    for name in ('TG_CLASS', 'TG_REPLY_TO', 'TG_LABEL'):
        env.pop(name, None)
    try:
        result = subprocess.run([str(SENDER), text], env=env, text=True, capture_output=True)
    except OSError:
        return Delivery('failed')
    lines = result.stdout.splitlines()
    code = int(lines[-1]) if lines and re.fullmatch(r'\d{3}', lines[-1]) else 0
    http = code or None
    return Delivery('sent' if result.returncode == 0 and http == 200 else 'failed', http)


def deliver(sender, text, *, label, root=None):
    try:
        _sender(sender)
        if label not in LABELS:
            raise ValueError('invalid label')
        validate_message(text, label)
    except (TypeError, ValueError):
        try:
            _audit(root, sender if isinstance(sender, str) else '',
                   text if isinstance(text, str) else '', 'refused', uuid.uuid4().hex,
                   reason='invalid automated message', kind='automated')
        except (OSError, ValueError):
            print('Telegram refusal audit unavailable', file=sys.stderr)
        return Delivery('refused')
    root = _root(root)
    if label in ('[MERGED]', '[DAILY]') and quiet(_now()):
        try:
            row = dict(id=uuid.uuid4().hex, sender=sender, text=text, label=label, enqueued=_now().isoformat())
            with _private(root / 'state/tg-held.lock'):
                _append(root / 'state/tg-held.jsonl', row)
        except (OSError, ValueError):
            return Delivery('failed')
        return Delivery('held')
    return _transport(sender, text, root)


def _load(root):
    path = root / 'state/tg-held.jsonl'
    if not path.exists() and not path.is_symlink():
        return []
    with _private(path) as inp:
        inp.seek(0)
        rows = [json.loads(line) for line in inp]
    ids = set()
    for row in rows:
        _sender(row['sender'])
        validate_message(row['text'], row['label'])
        if row['label'] not in ('[MERGED]', '[DAILY]') or not isinstance(row['id'], str) or row['id'] in ids:
            raise ValueError('invalid held record')
        instant = datetime.fromisoformat(row['enqueued'])
        quiet(instant)  # also checks awareness
        ids.add(row['id'])
    return rows


def _group(label, rows):
    total = len(rows)
    def render(kept):
        texts = [row['text'] for row in rows[:kept]]
        if kept < total:
            texts.append(f'and {total-kept} more held notifications omitted')
        return format_message(label, f'Held notifications: {total}', '\n\n'.join(texts),
                              'Read the held notices below for any action.', None, None)
    for kept in range(total, -1, -1):
        text = render(kept)
        if len(text.encode('utf-16-le')) // 2 <= 4096:
            return text
    raise ValueError('held summary exceeds transport limit')


def _ack(root, ids):
    # Serialize enqueue/read/replace; the drain lock separately serializes consumers.
    with _private(root / 'state/tg-held.lock'):
        rows = _load(root)
        remaining = [row for row in rows if row['id'] not in ids]
        path = root / 'state/tg-held.jsonl'
        scratch = path.with_name('tg-held.' + uuid.uuid4().hex)
        try:
            with _private(scratch) as out:
                for row in remaining:
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


def drain_held(now, root=None):
    if quiet(now):
        return []
    root = _root(root)
    try:
        with _private(root / 'state/tg-drain.lock'):
            with _private(root / 'state/tg-held.lock'):
                rows = _load(root)
            results = []
            for label in ('[MERGED]', '[DAILY]'):
                group = [row for row in rows if row['label'] == label]
                if not group:
                    continue
                result = _transport('drain-held', _group(label, group), root)
                if result.state == 'sent':
                    try:
                        _ack(root, {row['id'] for row in group})
                    except (OSError, ValueError, KeyError, TypeError):
                        print('held acknowledgement failed; remote acceptance is ambiguous', file=sys.stderr)
                        result = Delivery('failed', result.http)
                results.append(result)
            return results
    except (OSError, ValueError, KeyError, TypeError):
        print('cannot read or update held notifications', file=sys.stderr)
        return [Delivery('failed')]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ('--prepare', '--result'):
        try:
            if argv[0] == '--prepare':
                return _prepare(*argv[1:])
            _, sender, text, ident, code = argv
            _audit(None, sender, text, 'result', ident, int(code) or None)
            return 0
        except (OSError, ValueError):
            print('Telegram audit unavailable', file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description='Send a named operator notification')
    for field in ('sender', 'label', 'what', 'real', 'you'):
        parser.add_argument('--' + field, required=True)
    for field in ('by', 'doing'):
        parser.add_argument('--' + field)
    args = parser.parse_args(argv)
    try:
        text = format_message(args.label, args.what, args.real, args.you, args.by, args.doing)
        result = deliver(args.sender, text, label=args.label)
    except ValueError:
        result = Delivery('refused')
    print(json.dumps(asdict(result)))
    return 0 if result.state in ('sent', 'held') else 1


if __name__ == '__main__':
    raise SystemExit(main())
