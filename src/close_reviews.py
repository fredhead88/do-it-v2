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
        replans = [fold.ts(e['ts']) for e in evs if e['type'] == 'charter-replan']
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
