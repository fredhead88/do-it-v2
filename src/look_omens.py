"""The eight ledger omens, ported from the operator's omens.py (2026-11-01).
Returns readings only. External reads use look's injectable runner and deadline;
imports never execute the legacy script, fetch refs, or write local/ledger state.
"""
import re
from datetime import datetime, timezone

import fold
import window_members

DEFAULTS = dict(near_cap_fraction=0.8, vanished_cap_multiple=3, vanished_max_h=48,
                shelf_silent_min=60, raven_warn_h=0.5, raven_critical_h=2,
                regrade_count=3, inbound_uncarried_h=2,
                checkout='/home/albert/do-it-v2',
                caps=dict(grader=15, reviewer=30, builder=90, **{'spec-writer': 30, 'spec-auditor': 15}))
OWNERS = {'near-cap': 'executor', 'vanished': 'executor', 'shelf': 'executor',
          'shelf-stuck': 'executor', 'raven': 'thinker', 'known-bug': 'thinker',
          'contamination': 'executor', 'contamination-handled': 'executor',
          'regrade-loop': 'thinker', 'owed-due': 'executor', 'owed-parked': 'thinker',
          'inbound-uncarried': 'thinker', 'doit-not-live': 'operator', 'member-verdict': 'executor', 'branch-refreshes': 'executor'}
# A corrects: header may name the rule itself, omen-<rule>, or configured class.
KNOWN_BUG = {'L-charter-0034': 'planner-attempts-stuck: up.py re-escalates after every decision (fix routed to L-charter-0042 R6; the charter itself awaits its closing review)'}


def stamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def run(events, specs, runner, now, cfg):
    cfg = {**DEFAULTS, **cfg, 'caps': {**DEFAULTS['caps'], **cfg.get('caps', {})}}
    deadline = cfg.get('_deadline', runner.clock() + 90)
    rows = []
    by = {}
    for e in events:
        by.setdefault(e.get('subject'), []).append(e)
    def age(value):
        t = stamp(value)
        return (now - t).total_seconds() / 3600 if t else 0
    def add(rule, key, detail, severity='WARN'):
        rows.append(dict(rule=rule, key=str(key), detail=detail, severity=severity))
    def call(method, *args):
        # Same two-attempt, deadline-gated read contract as look._call.
        for _ in range(2):
            remaining = deadline - runner.clock()
            if remaining <= 0:
                break
            try:
                return getattr(runner, method)(*args, min(10, remaining))
            except Exception:
                pass
        raise OSError('reading unavailable')

    starts = {e['spawn']: e for e in events if e.get('type') == 'spawn-started' and e.get('spawn')}
    ends = {e.get('spawn') for e in events if e.get('type') in ('spawn-done', 'spawn-failed')}
    for sp, e in starts.items():
        if sp in ends:
            continue
        role = re.sub(r'^L-|-\d+$', '', sp)
        cap = cfg['caps'].get(role)
        if not cap:
            continue
        subject = e.get('subject')
        try:
            text = call('dispatch_processes', role, subject)
            match = re.search(r'--timeout\s+(\d+)', text)
            if match and int(match[1]) > 0:
                cap = int(match[1])
        except OSError:
            pass  # legacy fallback is the role cap, not a silence of the rule
        minutes = age(e.get('ts')) * 60
        if minutes > cfg['vanished_cap_multiple'] * cap:
            if minutes < cfg['vanished_max_h'] * 60:
                add('vanished', sp, f'{sp} ({subject}) started {minutes:.0f}m ago, no terminal event')
        elif minutes > cfg['near_cap_fraction'] * cap:
            add('near-cap', sp, f'{sp} ({subject}) at {100 * minutes / cap:.0f}% of its {cap}m cap',
                'CRIT' if minutes > cap else 'WARN')

    try:
        import autodispatch
        shelf = autodispatch.candidates(events, specs, now)
    except Exception:
        add('shelf', 'unavailable', 'shelf check unavailable (dispatcher error)')
    else:
        waiting, stuck = [], {}
        def last_h(sid):
            xs = by.get(sid) or []
            return age(xs[-1].get('ts')) if xs else 999
        for r in shelf:
            match = re.search(r'wave-blocked by (L-spec-\d+)', r.get('reason') or '')
            head, seen = (match[1] if match else None), set()
            while head and head not in seen:
                seen.add(head)
                nxt = next((x for x in shelf if x['spec'] == head), {})
                match = re.search(r'wave-blocked by (L-spec-\d+)', nxt.get('reason') or '')
                if not match:
                    break
                head = match[1]
            if not head:
                if r.get('status') == 'dispatchable' and last_h(r['spec']) > cfg['shelf_silent_min'] / 60:
                    stuck.setdefault(r['spec'], []).append(r['spec'])
                else:
                    waiting.append(r['spec'])
            elif last_h(head) > cfg['shelf_silent_min'] / 60:
                stuck.setdefault(head, []).append(r['spec'])
            else:
                waiting.append(r['spec'])
        for head, deps in sorted(stuck.items()):
            add('shelf-stuck', head, f'{head} silent {last_h(head):.1f}h and holding {len(deps)} spec(s): {", ".join(deps)}')
        if waiting:
            add('shelf', 'waiting', f'{len(waiting)} specs waiting their turn behind active work', 'INFO')

    bysrc = {e.get('_src'): e for e in events if e.get('_src')}
    for q in fold.open_questions(events):
        h = age(bysrc.get(q.get('src'), {}).get('ts'))
        subject = q.get('subject')
        if subject in KNOWN_BUG:
            add('known-bug', q.get('src') or subject, f'{subject} open {h:.1f}h: {KNOWN_BUG[subject]}', 'INFO')
        elif h > cfg['raven_warn_h']:
            add('raven', q.get('src') or subject,
                f'{subject} {q.get("kind")} open {h:.1f}h: {str(q.get("asks"))[:90]}',
                'CRIT' if h > cfg['raven_critical_h'] else 'WARN')

    for sid, spec in specs.items():
        evs = by.get(sid, [])
        voids = [e for e in evs if e.get('type') == 'spawn-failed' and 'contamination' in str(e.get('why', ''))
                 and 'identical packet' not in str(e.get('why', ''))]
        state = spec['state']
        if voids and state in ('built', 'graded', 'reviewing', 'building'):
            latest = voids[-1].get('ts', '')
            later = [e for e in evs if e.get('type') == 'verdict' and e.get('ts', '') > latest]
            fixed = [e for e in evs if e.get('type') == 'spec-written' and e.get('reasserted') and e.get('ts', '') > latest]
            if not later and fixed:
                add('contamination-handled', sid, f'{sid}: regrade note reasserted {fixed[-1].get("ts", "")[11:16]}Z, awaiting a fresh grade', 'INFO')
            elif not later:
                add('contamination', sid, f'{sid}: {len(voids)} contamination void(s), no grade since', 'CRIT')
        n = sum(e.get('type') == 'verdict' for e in evs)
        if n >= cfg['regrade_count'] and not state.startswith(('shipped', 'accepted', 'killed', 'dropped', 'void', 'closed')):
            add('regrade-loop', sid, f'{sid} graded {n}x, state {state}')

    previous_now = fold.NOW
    try:
        fold.NOW = now
        due = fold.owed_due(specs)
    finally:
        fold.NOW = previous_now
    real = [o for o in due if specs.get(o['spec'], {}).get('state', '').startswith(('shipped', 'accepted'))]
    try:
        # _load_orphans was retired on main; due_candidates owns current exclusions.
        import sweep_owed
        actionable = sweep_owed.due_candidates(specs, now)
    except Exception:
        actionable = None
    if actionable is None:
        if due:
            add('owed-due', 'owed', f'{len(real)} real owed checks past due (sweeper view unavailable)')
    elif actionable:
        oldest = min(r['due_at'] for r in actionable)
        parked = max(0, len(real) - len(actionable))
        add('owed-due', 'owed', f'{len(actionable)} owed checks due that the sweeper can act on (oldest due {str(oldest)[:16]}); {parked} more parked by decision')
    elif real:
        add('owed-parked', 'owed', f'{len(real)} owed checks past due but parked by decision or unobservable; they clear when charter 0042 widens sweeper access', 'INFO')

    registered, carried = {}, set()
    for e in events:
        source = str(e.get('source') or e.get('subject') or '')
        if 'pull/' not in source:
            continue
        if e.get('type') == 'inbound-registered':
            registered.setdefault(source, e.get('ts'))
        if e.get('type') in ('spec-carried', 'inbound-covered', 'inbound-closed'):
            carried.add(source)
    stale = [s for s, t in registered.items() if s not in carried and age(t) > cfg['inbound_uncarried_h']]
    if stale:
        add('inbound-uncarried', 'inbound', f'{len(stale)} inbound PRs registered >{cfg["inbound_uncarried_h"]}h, never carried')

    try:
        behind = int(call('git', ['rev-list', '--count', 'HEAD..origin/main'], cfg['checkout']).strip())
        if behind > 0:
            add('doit-not-live', 'do-it-v2', f'do-it-v2 checkout is {behind} merge(s) behind origin/main', 'CRIT')
    except (OSError, ValueError, AttributeError):
        add('doit-not-live', 'do-it-v2', 'could not check: reading unavailable')
    try:
        for rule, key, detail, severity in window_members.waiting_rows(events, specs, now):
            add(rule, key, detail, severity)
    except Exception:
        add('member-verdict', 'unavailable', 'member-verdict reading unavailable')
    from branch_refresh import utc_day
    count = sum(e.get('type') == 'branch-refreshed' and utc_day(e['ts']) == now.astimezone(timezone.utc).date()
                for e in events)
    if count:
        add('branch-refreshes', str(now.astimezone(timezone.utc).date()), f'{count} branch refresh(es) today (UTC)', 'INFO')
    order = {'CRIT': 0, 'WARN': 1, 'INFO': 2}
    return sorted(rows, key=lambda r: (order[r['severity']], r['rule'], r['key']))


def fix_for(rule, root, events, classes):
    import pathlib
    import relay
    tokens = {rule, 'omen-' + rule, classes.get('omen-' + rule), classes.get(rule)} - {None}
    # Owed omens share the established "owed" class.
    if rule in ('owed-due', 'owed-parked'):
        tokens.add('owed')
    for cid in relay.open_charters(events, root=root):
        try:
            text = (pathlib.Path(root) / 'content' / f'{cid}.md').read_text()[:4000].split('<!--', 1)[0]
        except OSError:
            continue
        match = re.search(r'^corrects:\s*(.+)$', text, re.MULTILINE)
        if match and tokens.intersection(v.strip() for v in match[1].split(',')):
            return cid
    return None
