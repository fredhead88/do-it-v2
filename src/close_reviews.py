"""Tick-owned charter close reviews, using admitted events and existing dispatch.

The tick receipt is a fork observation, never an assertion that a child started.
No private cache or second ledger is needed to reconcile it on the next pass.
"""
from collections import defaultdict
from datetime import timedelta
import json
import math
import os
from pathlib import Path
import re

import fold
import spawns

COOLDOWN = timedelta(hours=24)
_FORK = re.compile(r'^forked (L-charter-\d+) pid=([1-9]\d*) load=(\S+) open=(\d+) at=(\S+)$')
_FAILED = re.compile(r'^failed (L-charter-\d+) at=(\S+) reason=\S.*$')


def receipts(events):
    """Parse only durable tick outcomes; malformed rows confer no evidence."""
    out = []
    for event in events:
        if event.get('type') != 'tick':
            continue
        lines = event.get('close_reviews_outcomes', [])
        if not isinstance(lines, list):
            continue
        for line in lines:
            if not isinstance(line, str):
                continue
            m = _FORK.fullmatch(line)
            f = _FAILED.fullmatch(line)
            try:
                if m:
                    subject, pid, load, count, at = m.groups()
                    load = float(load)
                    if not math.isfinite(load) or load < 0:
                        continue
                    row = dict(kind='forked', subject=subject, pid=int(pid), load=load,
                               open=int(count), at=fold.ts(at))
                elif f:
                    row = dict(kind='failed', subject=f[1], at=fold.ts(f[2]))
                else:
                    continue
                out.append(row)
            except (ValueError, TypeError, OverflowError):
                continue
    return out


def standing_close(evs):
    closed = max((fold.ts(e['ts']) for e in evs if e['type'] == 'charter-closed'), default=None)
    reopened = max((fold.ts(e['ts']) for e in evs if e['type'] == 'charter-reopened'), default=None)
    return closed is not None and (reopened is None or reopened <= closed)


def _evidence(events, now):
    starts = [e for e in spawns.start_events(events) if spawns._role(e) == 'charter-reviewer']
    by_spawn = {e.get('spawn'): e for e in starts if e.get('spawn')}
    failures = {}

    def failed(subject, at):
        if subject:
            failures[subject] = max(at, failures.get(subject, at))

    for e in events:
        if e['type'] != 'spawn-failed':
            continue
        start = by_spawn.get(e.get('spawn'), {})
        if spawns._role(e) != 'charter-reviewer' and not start:
            continue
        subject = start.get('subject') or e.get('subject')
        failed(subject, fold.ts(e['ts']))
    forks = {}
    for receipt in receipts(events):
        subject = receipt['subject']
        if receipt['kind'] == 'failed':
            failed(subject, receipt['at'])
        else:
            key = (subject, receipt['pid'], receipt['at'])
            forks[key] = receipt
    pending, observed = [], []
    for receipt in forks.values():
        subject, pid, at = receipt['subject'], receipt['pid'], receipt['at']
        match = next((s for s in starts if s.get('subject') == subject
                      and s.get('waiter_pid') == pid), None)
        if match:
            observed.append((receipt, match))
        else:
            # An actual child refusal supersedes the no-start inference. A late
            # matching start removes the inference entirely on the next read.
            failure = failures.get(subject)
            if failure is None or failure < at:
                failed(subject, at)
                if now < at + COOLDOWN:
                    pending.append(receipt)
    open_ = [s for s in spawns.open_spawns(events, now) if s['role'] == 'charter-reviewer']
    return failures, pending, observed, open_


def occupied(events, now):
    """Global seats, deduplicating fork receipts against matching starts."""
    _, pending, _, open_ = _evidence(events, now)
    return len(open_) + len(pending)


def valid_replan_row(e):
    return (e.get('type') in ('charter-replan', 'charter-replan-held', 'charter-replan-released')
            and fold.admits(e, e.get('actor')) is None
            and fold.required_reason(e) is None)


def replan_state(events, now=None, *, root=None):
    """Replay active intervals, preserving incumbents when a held replan resumes.

    Pending releases wait oldest-verdict first. Readiness and the producer use
    this same replay, so releasing a hold cannot create a third active slot.
    """
    if not any(valid_replan_row(e) for e in events):
        return {}
    from itertools import groupby
    from relay import registered_projects
    projects = registered_projects(root)
    relevant = {'charter-replan', 'charter-replan-held', 'charter-replan-released',
                'cut-written', 'l1-complete', 'charter-closed', 'charter-reopened',
                'charter-retracted', 'charter-filed', 'charter-review-not-complete',
                'charter-review-complete'}
    rows = [e for e in events if e.get('type') in relevant
            and isinstance(e.get('ts'), str)
            and (not e['type'].startswith('charter-replan') or valid_replan_row(e))
            and (now is None or fold.ts(e['ts']) <= now)]
    verdicts = {e.get('_src'): fold.ts(e['ts']) for e in events
                if e.get('type') == 'charter-review-not-complete'}
    ended = {}
    for e in events:
        if e.get('type') in spawns.TERMINAL and e.get('spawn'):
            ended[e['spawn']] = min(fold.ts(e['ts']), ended.get(e['spawn'], fold.ts(e['ts'])))
    states, active, serviced = {}, set(), set()
    for at, batch in groupby(sorted(rows, key=lambda e: fold.ts(e['ts'])), lambda e: fold.ts(e['ts'])):
        for e in batch:
            cid, kind = e.get('subject'), e['type']
            s = states.setdefault(cid, dict(replan=None, marker=None, hold=None, release=None,
                                            closed=None, reopened=None, retracted=False,
                                            filed={}, verdict={}))
            if kind == 'charter-replan':
                serviced.add(e['verdict_src'])
                if s['replan'] is None or fold.ts(s['replan']['ts']) < at:
                    s['replan'] = e
            elif kind == 'charter-filed':
                s['filed'] = e
            elif kind in ('charter-review-not-complete', 'charter-review-complete'):
                if (not s['verdict'] or (at, e.get('_src', '')) >=
                        (fold.ts(s['verdict']['ts']), s['verdict'].get('_src', ''))):
                    s['verdict'] = e
            elif kind in ('cut-written', 'l1-complete'):
                s['marker'] = at
            elif kind == 'charter-replan-held':
                s['hold'] = e
            elif kind == 'charter-replan-released':
                s['release'] = at
            elif kind == 'charter-retracted':
                s['retracted'] = True
            elif kind == 'charter-closed':
                s['closed'] = at
            elif kind == 'charter-reopened':
                s['reopened'] = at
        for cid, s in states.items():
            r, hold = s['replan'], s['hold']
            s['held'] = bool(hold and (s['release'] is None or s['release'] <= fold.ts(hold['ts'])))
            closed = s['closed'] is not None and (s['reopened'] is None or s['reopened'] <= s['closed'])
            s['eligible'] = (not closed and not s['retracted']
                             and s['filed'].get('project') in projects)
            # Equality cannot end an active interval, but cannot re-enter a
            # plan already completed at that same instant either.
            s['pending'] = bool(r and (s['marker'] is None or fold.ts(r['ts']) > s['marker']
                                      or (cid in active and fold.ts(r['ts']) == s['marker']))
                                and not closed and not s['retracted'])
        active &= {cid for cid, s in states.items() if s['pending'] and s['eligible'] and not s['held']}
        waiting = [cid for cid, s in states.items() if s['pending'] and s['eligible']
                   and not s['held'] and cid not in active]
        pool = [(verdicts.get(states[cid]['replan']['verdict_src'], fold.ts(states[cid]['replan']['ts'])),
                 cid, states[cid]['replan']['verdict_src'], True) for cid in waiting]
        # Unserviced verdicts compete with waiting releases. Reserve their place
        # without pretending their event has already been appended. Replaying
        # the actual append later replaces that reservation with the same slot.
        for cid, s in states.items():
            v = s['verdict']
            source, spawn = v.get('_src'), v.get('spawn')
            if (isinstance(cid, str) and re.fullmatch(r'L-charter-[0-9]{4}', cid)
                    and s['eligible'] and not s['held'] and not s['pending']
                    and v.get('type') == 'charter-review-not-complete'
                    and isinstance(source, str) and source.strip() and source not in serviced
                    and (not spawn or (spawn in ended and ended[spawn] <= at))):
                pool.append((fold.ts(v['ts']), cid, source, False))
        active.update(cid for _, cid, _, recorded in sorted(pool)[:max(0, 2-len(active))] if recorded)
    for cid, s in states.items():
        s['active'] = cid in active
    return states


def replans_due(events, now):
    """Pure selection from admitted standing verdicts, including historical runs."""
    from relay import registered_projects
    by_subject = fold.fold(events)[3]
    admitted = [e for rows in by_subject.values() for e in rows if fold.ts(e['ts']) <= now]
    states = replan_state(admitted, now)
    available = max(0, 2-sum(s['active'] for s in states.values()))
    serviced = {e['verdict_src'] for e in admitted if e['type'] == 'charter-replan' and valid_replan_row(e)}
    ended = {e.get('spawn') for e in admitted if e['type'] in spawns.TERMINAL}
    projects = registered_projects(fold.ROOT)
    candidates = []
    for cid, evs in by_subject.items():
        if not isinstance(cid, str) or not re.fullmatch(r'L-charter-[0-9]{4}', cid):
            continue
        evs = [e for e in evs if fold.ts(e['ts']) <= now]
        filed = next((e for e in reversed(evs) if e['type'] == 'charter-filed'), {})
        if (filed.get('project') not in projects or standing_close(evs)
                or any(e['type'] == 'charter-retracted' for e in evs)
                or states.get(cid, {}).get('held') or states.get(cid, {}).get('pending')):
            continue
        verdict = max((e for e in evs if e['type'] in ('charter-review-complete', 'charter-review-not-complete')),
                      key=lambda e: (fold.ts(e['ts']), e.get('_src', '')), default=None)
        if not verdict or verdict['type'] != 'charter-review-not-complete':
            continue
        source = verdict.get('_src')
        if not isinstance(source, str) or not source.strip():
            raise ValueError('replan missing verdict identity')
        if source in serviced:
            continue
        spawn = verdict.get('spawn')
        if spawn and spawn not in ended:
            continue
        uncovered = verdict.get('uncovered_requirement_ids')
        if not isinstance(uncovered, list) or not all(isinstance(r, str) for r in uncovered):
            raise ValueError('replan malformed uncovered requirement ids')
        ledger = source.rpartition(':')[0]
        findings = [dict(e) for e in evs if e['type'] == 'audit-finding'
                    and e.get('actor') == 'charter-reviewer'
                    and ((spawn and e.get('spawn') == spawn)
                         or (not spawn and ledger and e.get('_src', '').rpartition(':')[0] == ledger))]
        body = dict(subject=cid, verdict_src=source, findings=findings, uncovered=list(uncovered))
        candidates.append((fold.ts(verdict['ts']), cid, source, body))
    return [body for _, _, _, body in sorted(candidates)[:available]]


def due(events, specs, now):
    """Eligible charter IDs from the pass snapshot, without load or paid work."""
    by_subject = defaultdict(list)
    for e in events:
        by_subject[e.get('subject')].append(e)
    failures, pending, _, open_ = _evidence(events, now)
    busy = {s['subject'] for s in open_} | {r['subject'] for r in pending}
    out = []
    for cid, evs in by_subject.items():
        filed = [e for e in evs if e['type'] == 'charter-filed']
        if not filed or any(e['type'] == 'charter-retracted' for e in evs):
            continue
        if standing_close(evs) or not fold.charter_review_owed(evs) or cid in busy:
            continue
        if cid in failures and now < failures[cid] + COOLDOWN:
            continue
        markers = [fold.ts(e['ts']) for e in evs if e['type'] in ('l1-complete', 'cut-written')]
        replans = [fold.ts(e['ts']) for e in evs if e['type'] == 'charter-replan' and valid_replan_row(e)]
        if not markers or (replans and max(markers) <= max(replans)):
            continue
        mine = [s for s in specs.values() if s.get('charter') == cid and s['state'] != 'void']
        if not mine:
            continue
        ids = {s['id'] for s in mine}
        shipments = [e for e in events if e['type'] == 'shipped' and e.get('subject') in ids]
        shipped_ids = {e['subject'] for e in shipments}
        if any(s['id'] not in shipped_ids and s['state'] not in ('killed', 'dropped', 'closed-unbuilt')
               for s in mine):
            continue
        verdicts = [e for e in evs if e['type'] in ('charter-review-complete', 'charter-review-not-complete')]
        if verdicts and verdicts[-1]['type'] == 'charter-review-not-complete':
            if not any(fold.ts(e['ts']) > fold.ts(verdicts[-1]['ts']) for e in shipments):
                continue
        out.append((min(fold.ts(e['ts']) for e in filed), cid))
    return [cid for _, cid in sorted(out)]


def run(events, specs, now, *, runner, dry_run=False):
    """Admit close-review forks under the tick lock; return durable receipts.

    Diagnostics deliberately use fixed reasons, never subprocess output, charter
    contents, or exception messages. One candidate's transport failure cannot
    erase an earlier candidate's fork receipt.
    """
    lines = []
    try:
        return _run(events, specs, now, runner=runner, dry_run=dry_run, lines=lines)
    except Exception as exc:
        # The integration boundary persists this list even if a later global
        # observation fails after an earlier detachment already happened.
        error = RuntimeError('close-review-pass-failed:' + type(exc).__name__)
        error.outcomes = list(lines)
        raise error from exc


def _run(events, specs, now, *, runner, dry_run, lines):
    failures, pending, observed, open_ = _evidence(events, now)
    for receipt, start in observed:
        lines.append(f"observed-start {receipt['subject']} pid={receipt['pid']} spawn={start.get('spawn')}")
    held = {cid for cid, at in failures.items() if now < at + COOLDOWN}
    held |= {r['subject'] for r in pending}
    for cid in sorted(held):
        lines.append(f'held {cid} reason=cooldown')
    count = len(open_) + len(pending)
    at = now.isoformat()
    candidates = due(events, specs, now)
    for cid in candidates:
        if count >= 2:
            lines.append(f'held {cid} reason=capacity')
            continue
        if dry_run:
            lines.append(f'would-dispatch {cid}')
            count += 1
            continue
        try:
            load = float(os.getloadavg()[0])
        except (OSError, ValueError, TypeError, IndexError):
            lines.append(f'held {cid} reason=load-unavailable')
            continue
        if not math.isfinite(load) or load < 0 or load > 20:
            lines.append(f'held {cid} reason=load')
            continue
        reason = 'metadata'
        try:
            filed = next(e for e in reversed(events) if e['type'] == 'charter-filed' and e.get('subject') == cid)
            project = filed.get('project')
            spawns.default_cwd('charter-reviewer', cid, project, fold.ROOT)
            value = filed.get('path')
            if not isinstance(value, str) or not value:
                raise ValueError('path')
            path = Path(value)
            if not path.is_absolute():
                path = fold.ROOT / path
            if not path.is_file():
                raise ValueError('path')
            reason = 'packet'
            packet = runner(['doit', 'packet', 'charter-reviewer', cid, '--charter', str(path)])
            packet_path = packet.get('stdout', '').strip()
            if packet.get('code', 1) != 0 or not packet_path or '\n' in packet_path or '\r' in packet_path:
                raise ValueError('packet')
            reason = 'dispatch'
            result = runner(['doit', 'dispatch', '--detach', 'charter-reviewer', cid,
                             '--packet', packet_path, '--project', project, '--charter', cid])
            if result.get('code', 1) != 0:
                raise ValueError('dispatch')
            reason = 'detached-response'
            response = json.loads(result.get('stdout', ''))
            pid = response.get('detached')
            if (type(pid) is not int or pid <= 0 or response.get('role') != 'charter-reviewer'
                    or response.get('subject') != cid or not isinstance(response.get('log'), str)):
                raise ValueError('detached-response')
            lines.append(f'forked {cid} pid={pid} load={load} open={count} at={at}')
            count += 1
        except Exception:
            lines.append(f'failed {cid} at={at} reason={reason}')
    return lines
