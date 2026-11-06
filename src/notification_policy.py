"""Owned interruption rules; maintenance readings belong in the daily summary."""
from datetime import timedelta
import math

import digest_actions
import owed_routes


def current_operator(row, now):
    try:
        age = now-digest_actions.instant(row['since'])
    except (KeyError,TypeError,ValueError):
        return False
    return (row.get('owner') == 'operator' and not row.get('decided')
            and timedelta(0) <= age <= timedelta(hours=48)
            and owed_routes.classify_ask(row.get('ask'),row.get('route'))['owner'] == 'operator')


def health_interrupt(alarm):
    """Severity alone is not evidence of downtime or money loss."""
    reading=alarm.get('reading',{})
    loss=reading.get('money_loss')
    return (reading.get('production_down') is True or
            (isinstance(loss,(float,int)) and not isinstance(loss,bool) and math.isfinite(loss) and loss>0))


def bill_interrupt(decision):
    """Measured cost threshold alarms can interrupt; routine switches and missing reads cannot."""
    return (decision.get('action') in ('alert','alert-50','alert-75','alert-90')
            and any(isinstance(decision.get(k),(int,float)) and not isinstance(decision.get(k),bool)
                    and math.isfinite(decision[k]) and decision[k]>0 for k in ('yday','mtd')))


# This prefix is the only label channel shared by formatter and transport.
LABELS = ('[YOU]', '[DOWN]', '[MERGED]', '[DAILY]')


def validate_text(text):
    """Reject local links/paths and UTC string stamps, without guessing dates."""
    import ipaddress
    import re
    from urllib.parse import urlsplit
    if not isinstance(text, str) or not text.strip():
        raise ValueError('blank message field')
    if re.search(r'\bUTC\b|\+00:00|\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z\b', text, re.I):
        raise ValueError('use Israel time')
    if re.search(r'(?im)\bYou:\s*(?:$|\n)', text):
        raise ValueError('blank You clause')
    # Remove hosted links before detecting bare absolute local paths.
    def hosted(match):
        value = urlsplit(match.group())
        host = value.hostname or ''
        if value.scheme != 'https' or not host or host.lower() == 'localhost' or host.lower().endswith('.localhost') or '.' not in host or value.username or value.password:
            raise ValueError('use a hosted HTTPS link')
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not ip.is_global:
                raise ValueError('use a hosted HTTPS link')
        return 'link'
    remaining = re.sub(r'\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>]+', hosted, text)
    if re.search(r'(?<![\w/])(?:/[^\s/]|~/|[A-Za-z]:[\\/])|\blocalhost(?::\d+)?(?:/|\b)', remaining, re.I):
        raise ValueError('local paths are not hosted links')
    return text


def message_label(text):
    if isinstance(text, str):
        for label in LABELS:
            if text.startswith(label + ' '):
                return label
    raise ValueError('missing message label')


def validate_message(text, label=None):
    import re
    parsed = message_label(text)
    if label is not None and parsed != label:
        raise ValueError('message label mismatch')
    validate_text(text)
    if not re.search(r'(?m)^You:[ \t]*\S', text):
        raise ValueError('missing You clause')
    return parsed


def format_message(label, what, real, you, by=None, doing=None):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    if label not in LABELS:
        raise ValueError('invalid label')
    for field in (what, real, you):
        validate_text(field)
    if isinstance(by, datetime):
        if by.tzinfo is None or by.utcoffset() is None:
            raise ValueError('deadline needs a timezone')
        by = by.astimezone(ZoneInfo('Asia/Jerusalem')).strftime('%Y-%m-%d %H:%M')
    fields = [label + ' ' + what, real, 'You: ' + you]
    for name, value in (('By', by), ('Doing', doing)):
        if value is not None and value != '':
            validate_text(value)
            fields.append(name + ': ' + value)
    text = '\n'.join(fields)
    validate_message(text, label)
    return text
