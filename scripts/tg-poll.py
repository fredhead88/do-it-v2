#!/usr/bin/env python3
"""Prompted Telegram replies and literal operator relay; runtime state is private.

Only getUpdates/getFile/downloads use HTTP here. The repository's shared sender
owns every reply and its audit. HOME owns external helpers; DOIT_ROOT owns state.
"""
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

D = Path(os.environ.get('DOIT_ROOT', Path.home() / '.do-it')).absolute()
T = D / 'telegram'
LOG = D / 'logs/telegram.log'
OFF = T / 'offset'
CHAT = T / 'chat_id'
INBOX = T / 'inbox'
SENDER = Path(__file__).resolve().parent / 'tg-send.sh'
PULSE = Path.home() / '.do-it/telegram/pulse.py'
SWITCH = Path.home() / '.do-it/bin/claude-switch.sh'
SESSION = Path.home() / '.do-it/state/flow-sessions.env'
PANE = 'flow:2.0'
MAX_BYTES = 20 * 1024 * 1024
ISRAEL = ZoneInfo('Asia/Jerusalem')


def private_dir(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink():
        raise OSError('unsafe state directory')
    path.chmod(0o700)


def private_write(path, data):
    """Publish only complete, fsynced 0600 files; retain old state on failure."""
    private_dir(path.parent)
    fd, scratch = tempfile.mkstemp(prefix='.poll-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(scratch, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(scratch).unlink(missing_ok=True)


def stamp(instant=None):
    return (instant or datetime.datetime.now(ISRAEL)).astimezone(ISRAEL).strftime('%H:%M')


def log(event):
    # Callers supply fixed diagnostics/counts, never exception text, URLs or inputs.
    private_dir(LOG.parent)
    fd = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as out:
        os.fchmod(out.fileno(), 0o600)
        out.write(f'{datetime.datetime.now(ISRAEL):%Y-%m-%d %H:%M:%S} {event}\n')


def api(tok, method, **p):
    url = f'https://api.telegram.org/bot{tok}/{method}?' + urllib.parse.urlencode(p)
    with urllib.request.urlopen(url, timeout=70) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise ValueError('inbound request failed')
    return result


def safe_reply(text, tok):
    """External helpers are untrusted; withhold private output as a whole."""
    # Hosted links are permitted; other slash-prefixed references are local.
    without_links = re.sub(r'https?://[^\s<>]+', '', text)
    if (tok and tok in text) or re.search(
        r'api\.telegram\.org|\b(?:token|password|secret|authorization|dsn|api[_-]?key|access[_-]?key)[\"\x27]?\s*[:=]|\bBearer\s+'
        r'|(?:postgres(?:ql)?|mysql|redis)://'
        r'|https?://[^\s/]+@|\b\d{6,}:[A-Za-z0-9_-]{20,}'
        r'|\b\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|\+00:00)\b|\bUTC\b', text, re.I
    ) or re.search(r'(?<![\w/])/(?!/)|file://|~/', without_links):
        return 'Helper output withheld because it contains private data.'
    encoded = text.encode('utf-16-le')
    if len(encoded) > 7800:
        kept = encoded[:7600].decode('utf-16-le', errors='ignore')
        return kept + f'\n[{len(text) - len(kept)} characters omitted: reply length limit]'
    return text


def reply(update_id, text, tok):
    try:
        result = subprocess.run(
            [str(SENDER), safe_reply(text, tok)],
            env={**os.environ, 'DOIT_ROOT': str(D), 'TG_SENDER': 'tg-poll',
                 'TG_CLASS': 'reply', 'TG_REPLY_TO': str(update_id)},
            capture_output=True, text=True, timeout=40,
        )
        lines = result.stdout.splitlines()
        sent = result.returncode == 0 and bool(lines) and lines[-1] == '200'
    except (OSError, subprocess.SubprocessError):
        sent = False
    log('reply sent' if sent else 'reply failed: shared sender did not confirm HTTP 200')
    return sent


def thinker_pane():
    """Find the resumed Thinker even if its pane has been renumbered."""
    try:
        sid = next((line.split('=', 1)[1].strip() for line in SESSION.read_text().splitlines()
                    if line.startswith('THINKER_ID=')), '')
        if not sid:
            return PANE
        result = subprocess.run(['tmux', 'list-panes', '-a', '-F', '#{pane_id} #{pane_pid}'],
                                capture_output=True, text=True, timeout=10, check=True)
        for line in result.stdout.splitlines():
            pane, pid = line.split()
            tree = subprocess.run(['pstree', '-a', pid], capture_output=True, text=True, timeout=10, check=True)
            if '--resume ' + sid in tree.stdout:
                return pane
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return PANE


def fetch_attachment(tok, message):
    """Bound actual bytes before publishing a private, inert attachment."""
    if message.get('photo'):
        item, name = message['photo'][-1], 'photo.jpg'
    else:
        kind = next((k for k in ('document', 'video', 'voice', 'audio') if message.get(k)), None)
        if kind is None:
            return None
        item = message[kind]
        name = item.get('file_name') or {'document': 'file', 'video': 'video.mp4', 'voice': 'voice.ogg', 'audio': 'audio'}[kind]
    if (item.get('file_size') or 0) > MAX_BYTES:
        return '[attachment exceeds 20 MiB; not saved]'
    info = api(tok, 'getFile', file_id=item['file_id'])['result']
    remote = info['file_path']
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', remote) or remote.startswith('/') or any(p in ('.', '..') for p in remote.split('/')):
        raise ValueError('unsafe download path')
    with urllib.request.urlopen(f'https://api.telegram.org/file/bot{tok}/{remote}', timeout=120) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        log('attachment rejected: actual bytes exceed 20 MiB')
        return '[attachment exceeds 20 MiB; not saved]'
    safe = ''.join(c if c.isascii() and (c.isalnum() or c in '._-') else '_' for c in name)[-80:].lstrip('.') or 'file'
    private_dir(INBOX)
    # Random suffix prevents two updates or repeated message ids overwriting bytes.
    fd, local = tempfile.mkstemp(prefix='attachment-', suffix='-' + safe, dir=INBOX)
    dest = Path(local)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    log(f'attachment saved: {len(data)} bytes')
    return f'[attached file saved: {dest} ({len(data)} bytes)]'


def dispatch(tok, update_id, message):
    cid = str((message.get('chat') or {}).get('id', ''))
    text = (message.get('text') or message.get('caption') or '').strip()
    has_file = any(message.get(k) for k in ('photo', 'document', 'video', 'voice', 'audio'))
    if not re.fullmatch(r'-?[0-9]+', cid) or not (text or has_file):
        log('ignored 1 update: unsupported message')
        return
    if not CHAT.exists() and text.startswith('/start'):
        private_write(CHAT, cid.encode())
        log('chat claim persisted')
        reply(update_id, 'Linked to the Thinker. Message me here any time.', tok)
        return
    if not CHAT.exists() or CHAT.read_text().strip() != cid:
        log('ignored 1 update: foreign or unclaimed chat')
        return
    if text.startswith('/start'):
        return
    command = text.lower().strip(' /!?.')
    if command in ('pulse', 'status', 'p'):
        try:
            result = subprocess.run([sys.executable, str(PULSE)], capture_output=True, text=True, timeout=240)
            picture = (result.stdout.strip() or 'pulse: no output') if result.returncode == 0 else 'Pulse failed.'
        except (OSError, subprocess.SubprocessError):
            picture = 'Pulse failed.'
        reply(update_id, picture, tok)
        return
    switch = re.fullmatch(r'(switch|save)\s*(?:account\s*)?([12])', command)
    if switch:
        verb, account = switch.groups()
        try:
            cmd = [str(SWITCH), verb, account]
            if verb == 'switch':
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                answer = f'Switching Claude to account {account}. The Thinker, Executor and email panes restart in about a minute.'
            else:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                answer = (result.stdout.strip() or 'done') if result.returncode == 0 else 'Account save failed.'
        except (OSError, subprocess.SubprocessError):
            answer = 'Account helper failed.'
        reply(update_id, answer, tok)
        return
    note = None
    if has_file:
        try:
            note = fetch_attachment(tok, message)
        except (OSError, ValueError, KeyError, TypeError):
            note = '[attachment download failed; not saved]'
            log('attachment download failed; not saved')
    body = f'[Telegram from operator {stamp()} reply_to={update_id}] ' + ' '.join(text.splitlines())
    if note:
        body += ' ' + note
    try:
        pane = thinker_pane()
        typed = subprocess.run(['tmux', 'send-keys', '-t', pane, '-l', body], timeout=10, capture_output=True)
        if typed.returncode != 0:
            raise OSError('pane typing failed')
        time.sleep(1.5)
        entered = subprocess.run(['tmux', 'send-keys', '-t', pane, 'Enter'], timeout=10, capture_output=True)
        if entered.returncode != 0:
            raise OSError('pane Enter failed')
    except (OSError, subprocess.SubprocessError):
        log('pane delivery failed; no acknowledgement sent')
        return
    log('pane delivered')
    reply(update_id, 'Received.', tok)


def poll_once():
    tok = (D / 'telegram.token').read_text().strip()
    offset = int(OFF.read_text()) if OFF.exists() else 0
    result = api(tok, 'getUpdates', offset=offset, timeout=50)
    for update in result.get('result', []):
        uid = update.get('update_id')
        if type(uid) is not int or uid <= 0 or uid < offset:
            continue
        # Preserve the live consume-before-dispatch convention, including ignored
        # updates. This is not an exactly-once guarantee across a mid-update crash.
        private_write(OFF, str(uid + 1).encode())
        offset = uid + 1
        dispatch(tok, uid, update.get('message') or {})


def main():
    while True:
        try:
            poll_once()
        except Exception:
            # Never serialize exception text: HTTP/process errors may carry secrets.
            try:
                log('poll iteration failed; retrying in 15 seconds')
            except OSError:
                pass
            time.sleep(15)


if __name__ == '__main__':
    main()
