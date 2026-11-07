"""One Telegram summary owner for owed checks and stalled work (0051/R9, 0058/R3)."""
import fcntl
import json
import os
import subprocess
import re
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import digest_actions
import fold
import notification_policy
import owed_routes
from notification_policy import current_operator

ISRAEL = ZoneInfo("Asia/Jerusalem")
LIMIT = 3500


def key(row):
    return f"{row['spec']}/{row['criterion']}/{row['since']}"


def _date(now):
    return now.astimezone(ISRAEL).date().isoformat()


def _due(now):
    return now.astimezone(timezone.utc).hour == 6


def _active():
    return (fold.ROOT / 'state/tg-cutover-done').exists()


def _guarded(previous, now):
    return _date(now) in (previous.get('sent_date'), previous.get('held_date'))


def state(events, now):
    import owed_digest
    path = fold.ROOT / 'state/telegram-summary.json'
    obj = json.loads(path.read_text()) if path.exists() else dict(rows=[])
    if (not isinstance(obj, dict) or not isinstance(obj.get('rows'), list)
            or not all(isinstance(r, str) for r in obj['rows'])):
        raise ValueError('invalid Telegram summary state')
    for field in ('sent_date', 'held_date'):
        if field in obj:
            try:
                if datetime.strptime(obj[field], '%Y-%m-%d').date().isoformat() != obj[field]:
                    raise ValueError()
            except (TypeError, ValueError):
                raise ValueError('invalid Telegram summary date')
    legacy = owed_digest._alerted()
    obj['rows'] = sorted(set(obj['rows']) | set(legacy['rows']))
    # Preserve today's successful delivery during rollout, from either caller.
    date = _date(now)
    if legacy.get('sent_date') == date or any(
            e.get('type') == 'operator-alert' and e.get('kind') == 'stuck-digest'
            and e.get('sent') == 'yes' and e.get('date') == date
            and digest_actions.instant(e['ts']) <= now for e in events):
        obj['sent_date'] = date
    return obj


def plan(events, now, previous):
    return _plan(events, now, previous, _active())


def _plan(events, now, previous, active):
    import owed_digest
    import stuck_digest
    checks = owed_digest.build(events, now)
    work = stuck_digest.build(events, now)
    admitted = fold.fold(digest_actions.as_of(events, now))[3]
    sources = {e['_src']: e for evs in admitted.values() for e in evs if e.get('_src')}
    answers = list(sources.values()) + [e for e in events if e.get('type') in ('operator-approval','thinker-answer')
                                        and e.get('actor') in ('operator','thinker')]
    rows = list(checks['needs_you'])
    for raw in work['action_rows']:
        row = dict(raw, criterion='next step', since=raw['since'])
        row['kind'] = owed_routes.classify_ask(row.get('ask'))['kind']
        request = sources.get(row.get('src'), {})
        row['decided'] = digest_actions.answered(row, request, answers, now, sources)
        rows.append(row)
    silent = {key(r) for r in rows if not current_operator(r, now)}
    silent.update(key(r) for r in checks['answered_asks'] + checks['thinker_rows'])
    known = set(previous['rows']) | silent
    fresh = sorted((r for r in rows if current_operator(r,now) and key(r) not in known),
                   key=lambda r:(r['spec'],r['criterion'],r['since']))
    # One act can surface in both timers and check evidence; retain its exact check reference.
    unique = []
    for row in fresh:
        if not any(r['spec'] == row['spec'] and r['ask'] == row['ask'] for r in unique):
            unique.append(row)
    waiting = len({r['spec'] for r in work.get('waiting_rows',[])})
    thinker = len({r['spec'] for r in work['action_rows'] if not current_operator(r,now)})
    if active:
        return _activated(events, now, checks, waiting, thinker, unique, fresh, known)
    header = (f"Daily summary — {now.date().isoformat()}\n" + digest_actions.counts_line(checks)
              + f"\nWork: {waiting} waiting; {thinker} need Thinker follow-up."
              + f" System health: {len(checks.get('alarms',[]))} active notices.")
    if not unique:
        header += '\nNothing needs you today.'
    message, included = digest_actions.pack(header, ['NEEDS YOU: '+digest_actions.operator_line(r) for r in unique])
    return dict(message=message, rows=sorted(known),
                delivered=[key(r) for r in fresh if any(
                    r['spec'] == shown['spec'] and r['ask'] == shown['ask'] for shown in unique[:included])])


def _safe(value, cap=None):
    """Validate each untrusted field before composing formatter clauses."""
    text = str(value)
    try:
        notification_policy.validate_text(text)
        if (any(unicodedata.category(c) in ('Cc', 'Cf', 'Zl', 'Zp') for c in text)
                or re.search(r'\[(?:YOU|DOWN|MERGED|DAILY)\]|\b(?:You|By|Doing):', text, re.I)):
            raise ValueError('injected clause')
    except ValueError:
        return 'unsafe value unread'
    if cap is not None and len(text) > cap:
        text = text[:cap-1] + '…'
        try:
            notification_policy.validate_text(text)
        except ValueError:
            # Abbreviation may cut the host off a valid hosted URL.
            return 'value abbreviated; full detail omitted'
    return text


def _local(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return 'time unknown'
    return value.astimezone(ISRAEL).strftime('%Y-%m-%d %H:%M')


def machine_lines(events, now):
    """Read existing summary helpers only; failures are isolated by source."""
    import look
    import thinker_inbox
    import health_alarms
    lines = []
    try:
        lines.append(_safe(look.heartbeat_line(events, now)))
    except Exception:
        lines.append('Heartbeat: unread')
    try:
        # open_items uses fold's optional on-disk read cache. This summary
        # helper must also be safe in a dry-run on a pristine scratch root.
        cache_setting = os.environ.get('DOIT_READ_CACHE')
        try:
            os.environ['DOIT_READ_CACHE'] = '0'
            items = thinker_inbox.open_items()
        finally:
            if cache_setting is None:
                os.environ.pop('DOIT_READ_CACHE', None)
            else:
                os.environ['DOIT_READ_CACHE'] = cache_setting
        # An item from the future cannot contribute a current queue incident.
        current = []
        future = 0
        for item in items:
            try:
                if digest_actions.instant(item.get('ts')) > now:
                    future += 1
                    continue
            except (TypeError, ValueError):
                pass  # queue_line counts unknown ages truthfully.
            current.append(item)
        lines.append(_safe(thinker_inbox.queue_line(current, now)))
        if future:
            lines.append(f'Queue: {future} future items excluded until their event time.')
    except Exception:
        lines.append('Queue: unread')
    try:
        lines.extend(_safe(line) for line in health_alarms.summary_lines(events, now))
    except Exception:
        lines.append('Health: unread')
    return lines


def _news(now):
    rows, unknown, invalid = [], 0, 0
    excluded = dict(future=0, earlier=0, reported=0, informational=0)
    try:
        lines = (fold.ROOT / 'state/merge-news.jsonl').read_text().splitlines()
    except (OSError, UnicodeError):
        return None, 'unread'
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError()
            for field in ('time', 'kind', 'specs', 'context_specs', 'window', 'reason', 'text', 'disposition'):
                if field not in row:
                    raise ValueError()
            if row['kind'] not in ('merged', 'failed', 'progress', 'unknown') or row['disposition'] not in ('pending', 'sent', 'daily', 'diagnostic'):
                raise ValueError()
            for field in ('specs', 'context_specs'):
                if not isinstance(row[field], list) or not all(isinstance(s, str) and re.fullmatch(r'L-spec-\d+', s) for s in row[field]):
                    raise ValueError()
            if not isinstance(row['text'], str) or any(row[f] is not None and not isinstance(row[f], str) for f in ('window', 'reason')):
                raise ValueError()
            when = digest_actions.instant(row['time'])
            if when.tzinfo is None or when.utcoffset() is None:
                raise ValueError()
            if when > now:
                excluded['future'] += 1
                continue
            if _date(when) != _date(now):
                excluded['earlier'] += 1
                continue
            if row['kind'] == 'unknown':
                unknown += 1
            elif row['kind'] in ('merged', 'failed') and row['disposition'] in ('pending', 'daily'):
                identity = ', '.join(row['specs']) or 'Unattributed'
                details = '; '.join(_safe(row[f], 160) for f in ('reason', 'window') if row[f])
                rows.append(f"{identity}: {row['kind']}" + (f'; {details}' if details else ''))
            elif row['kind'] == 'progress':
                excluded['informational'] += 1
            else:
                excluded['reported'] += 1
        except (TypeError, ValueError, OverflowError):
            invalid += 1
    status = f'{unknown} unrecognised lines'
    if any(excluded.values()):
        status += '; excluded: '+', '.join(f'{count} {reason}' for reason, count in excluded.items() if count)
    if invalid:
        status += f'; partial: {invalid} unread lines'
    return rows, status


def _activated(events, now, checks, waiting, thinker, unique, fresh, known):
    import merge_loop_state
    try:
        day = merge_loop_state.day_lists(now, root=fold.ROOT)
    except Exception:
        day = None
    sections = []
    for name in ('merged', 'released', 'held'):
        values = getattr(day, name, None)
        if values is not None and not isinstance(values, list):
            values = None
        entries = None if values is None else []
        for row in values or []:
            try:
                if name == 'merged':
                    entries.append(f'{_safe(row.spec)}: {_safe(row.plain_line, 220)}; {_local(row.time)}')
                elif name == 'released':
                    entries.append(f'{_safe(row.version)}: {_safe(row.outcome)}; {_local(row.time)}')
                else:
                    age = 'age unknown' if row.age is None else f'{max(0, int(row.age.total_seconds()//3600))} hours'
                    entries.append(f'{_safe(row.spec)}: {_safe(row.reason, 220)}; {age}')
            except (AttributeError, TypeError, ValueError, OverflowError):
                entries.append('Row unread: malformed '+name+' data.')
        sections.append((name.capitalize(), entries, ''))
    try:
        loop = merge_loop_state.read(root=fold.ROOT, now=now)
        status = loop.status if loop.status in ('running', 'rejecting', 'stopped', 'undetermined') else 'undetermined'
        loop_line = f'Loop: {status}; {len(loop.queued)} queued; {len(loop.failures)} failures.'
    except Exception:
        loop_line = 'Loop: undetermined; unread'
    hold = getattr(day, 'release_hold', None)
    release_hold = ('yes' if hold else 'no') if isinstance(hold, bool) else 'unread'
    machine = machine_lines(events, now)
    deferred, news_status = _news(now)
    # Retain the exact ask, recommendation, default and check reference as one row.
    asks = ['NEEDS YOU: '+_safe(digest_actions.operator_line(row)) for row in unique]
    sections = [('Operator asks', asks, '')] + sections + [
        ('Machine', machine, ''), ('Deferred news', deferred, news_status)]
    base = (digest_actions.counts_line(checks) +
            f'\nWork: {waiting} waiting; {thinker} need Thinker follow-up.' +
            f" System health: {len(checks.get('alarms', []))} active notices.\n" +
            loop_line + f'\nRelease hold: {release_hold}')
    what = 'Daily summary — '+_date(now)
    included = [0]*len(sections)

    def fields():
        pieces = [base]
        for index, (name, entries, diagnostic) in enumerate(sections):
            heading = f'{name}: ' + ('unread' if entries is None else str(len(entries)))
            if diagnostic:
                heading += '; '+diagnostic
            pieces.append(heading)
            if entries is not None:
                pieces.extend(entries[:included[index]])
                missing = len(entries)-included[index]
                if missing:
                    pieces.append(f'and {missing} more')
        shown = sum('unsafe value unread' not in text for text in asks[:included[0]])
        you = (f'{len(asks)} pending operator asks; {shown} included above. Review their recommendations and defaults.'
               if asks else 'Nothing needs you today.')
        return '\n'.join(pieces), you

    # Budget complete rows fairly across sections. All counts and unread markers
    # are reserved first, including the actual formatter envelope.
    while True:
        grew = False
        for index, (_, entries, _) in enumerate(sections):
            if entries is None or included[index] >= len(entries):
                continue
            included[index] += 1
            real, you = fields()
            if len('[DAILY] '+what+'\n'+real+'\nYou: '+you) > LIMIT:
                included[index] -= 1
            else:
                grew = True
        if not grew:
            break
    real, you = fields()
    message = notification_policy.format_message('[DAILY]', what, real, you)
    assert len(message) <= LIMIT
    shown = [row for row, text in zip(unique[:included[0]], asks) if 'unsafe value unread' not in text]
    delivered = [key(r) for r in fresh if any(r['spec'] == s['spec'] and r['ask'] == s['ask'] for s in shown)]
    return dict(message=message, rows=sorted(known), delivered=delivered)


def preview(events, now):
    previous = state(events, now)
    if not _due(now) or _guarded(previous, now):
        return []
    return [plan(events, now, previous)['message']]


def deliver(now, sender=None):
    """Serialize both callers; only a confirmed send acknowledges shown asks."""
    import owed_digest
    import tg_say
    directory = fold.ROOT / 'state'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/'telegram-summary.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        active = _active()
        events = owed_digest._read_ledger()
        previous = state(events, now)
        planned = _plan(events, now, previous, active)
        previous['rows'] = planned['rows']
        path = directory/'telegram-summary.json'
        # Retire silent rows even outside the send window, as before.
        if not path.exists() or json.loads(path.read_text()) != previous:
            owed_digest._write_json(path, previous)
        if not _due(now) or _guarded(previous, now):
            return dict(sent=False, state=previous)
        if active:
            try:
                result = (sender or tg_say.deliver)('daily-summary', planned['message'], label='[DAILY]', root=fold.ROOT)
            except Exception as exc:
                raise OSError('daily summary transport interrupted; outcome unknown') from exc
            if result.state == 'held':
                previous['held_date'] = _date(now)
                owed_digest._write_json(path, previous)
                return dict(sent=False, state=previous)
            if result.state != 'sent':
                raise OSError('daily summary Telegram delivery '+str(result.state))
        else:
            argv = [str(fold.ROOT/'telegram/tg-send.sh'), planned['message']]
            result = sender(argv) if sender else subprocess.run(argv, capture_output=True, text=True,
                            env={**os.environ, 'TG_SENDER':'daily-summary'}, timeout=30)
            if result.returncode or (result.stdout or '').splitlines()[-1:] != ['200']:
                raise OSError('daily summary Telegram send failed')
        previous['rows'] = sorted(set(previous['rows']) | set(planned['delivered']))
        previous['sent_date'] = _date(now)
        owed_digest._write_json(path, previous)
        return dict(sent=True, state=previous, message=planned['message'])
