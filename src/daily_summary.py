"""One Telegram summary owner for owed checks and stalled work (0051/R9, 0058/R3)."""
import fcntl
import json
import os
import subprocess

import digest_actions
import fold
import owed_routes


from notification_policy import current_operator


def key(row):
    return f"{row['spec']}/{row['criterion']}/{row['since']}"


def state(events, now):
    import owed_digest
    path = fold.ROOT / 'state/telegram-summary.json'
    obj = json.loads(path.read_text()) if path.exists() else dict(rows=[])
    if not isinstance(obj.get('rows'), list):
        raise ValueError('invalid Telegram summary state')
    legacy = owed_digest._alerted()
    obj['rows'] = sorted(set(obj['rows']) | set(legacy['rows']))
    # Preserve today's successful delivery during rollout, from either caller.
    date = now.date().isoformat()
    if legacy.get('sent_date') == date or any(
            e.get('type') == 'operator-alert' and e.get('kind') == 'stuck-digest'
            and e.get('sent') == 'yes' and e.get('date') == date
            and digest_actions.instant(e['ts']) <= now for e in events):
        obj['sent_date'] = date
    return obj


def plan(events, now, previous):
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
    header = (f"Daily summary — {now.date().isoformat()}\n" + digest_actions.counts_line(checks)
              + f"\nWork: {waiting} waiting; {thinker} need Thinker follow-up."
              + f" System health: {len(checks.get('alarms',[]))} active notices.")
    if not unique:
        header += '\nNothing needs you today.'
    message, included = digest_actions.pack(header, ['NEEDS YOU: '+digest_actions.operator_line(r) for r in unique])
    return dict(message=message, rows=sorted(known),
                delivered=[key(r) for r in fresh if any(
                    r['spec'] == shown['spec'] and r['ask'] == shown['ask'] for shown in unique[:included])])


def preview(events, now):
    previous = state(events, now)
    if now.hour != 6 or previous.get('sent_date') == now.date().isoformat():
        return []
    return [plan(events, now, previous)['message']]


def deliver(now, sender=None):
    """Serialize both entrypoints; only HTTP 200 commits a successful daily send."""
    import owed_digest
    directory = fold.ROOT / 'state'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/'telegram-summary.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        events = owed_digest._read_ledger()
        previous = state(events, now)
        planned = plan(events, now, previous)
        previous['rows'] = planned['rows']
        path = directory/'telegram-summary.json'
        # Quietly acknowledge old/wrong-owner/answered rows even outside the daily hour.
        if not path.exists() or json.loads(path.read_text()) != previous:
            owed_digest._write_json(path, previous)
        if now.hour != 6 or previous.get('sent_date') == now.date().isoformat():
            return dict(sent=False, state=previous)
        argv = [str(fold.ROOT/'telegram/tg-send.sh'), planned['message']]
        result = sender(argv) if sender else subprocess.run(argv,capture_output=True,text=True,
                        env={**os.environ,'TG_SENDER':'daily-summary'},timeout=30)
        if result.returncode or (result.stdout or '').splitlines()[-1:] != ['200']:
            raise OSError('daily summary Telegram send failed')
        previous['rows'] = sorted(set(previous['rows']) | set(planned['delivered']))
        previous['sent_date'] = now.date().isoformat()
        owed_digest._write_json(path, previous)
        return dict(sent=True, state=previous, message=planned['message'])
