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
