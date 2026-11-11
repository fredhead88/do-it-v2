"""Multi-migration admission and parent records for the versioned window path.

The canonical order is migration_order.order's, the only bookkeeping authority
is rechain.resolve (reached through rechain_check.stack and the merge
fallbacks), and the parent records come from the resolver's own `rechained`
evidence bound to the final candidate. Nothing here writes a ref or an event.
"""
import os

import rechain


def enabled(env=None):
    return (os.environ if env is None else env).get('DOIT_WINDOW_RECHAIN', 'on') == 'on'


def adds_migration(repo, base, sha):
    try:
        return bool(rechain.added_versions(repo, base, sha))
    except Exception:
        return True  # undetermined: treat as a migration so ordering holds it


def canonical(repo, base, entries, events, content):
    """-> (entries in canonical order, {spec: hold reason}).

    Only specs that add a migration are reordered, within the positions they
    occupy; the order comes from migration_order.order untouched. A deferred,
    cyclic or unreadable spec is held, never admitted.
    """
    entries = list(entries)
    mig = [e for e in entries if adds_migration(repo, base, e[2])]
    if len(mig) < 2:
        return entries, {}
    try:
        import migration_order
        texts = {}
        for spec, _, _ in mig:
            try:
                texts[spec] = (content / (spec + '.md')).read_text()
            except OSError:
                pass
        result = migration_order.order([e[0] for e in mig], events, texts=texts)
    except Exception as exc:
        reason = 'migration order undetermined: ' + type(exc).__name__
        return [e for e in entries if e not in mig], {e[0]: reason for e in mig}
    held = dict(result.deferred)
    by_spec = {e[0]: e for e in mig}
    sequence = iter(by_spec[s] for s in result.ordered if s in by_spec)
    ordered = [next(sequence) if e in mig and e[0] not in held else e
               for e in entries if not (e in mig and e[0] in held)]
    return ordered, held


def stack_check(repo, base, entries):
    """-> (reason or None, set of specs the resolver re-chains).

    Every step must be clean or rechained; an exception, a missing step or an
    unknown outcome is undetermined. With re-chain disabled a re-chained step
    is a conflict, not permission.
    """
    if len(entries) < 2 or not any(adds_migration(repo, base, e[2]) for e in entries):
        return None, set()
    try:
        import rechain_check
        steps = rechain_check.stack(repo, base, entries)
    except Exception as exc:
        return 'migration stack undetermined: ' + type(exc).__name__, set()
    if [s.spec for s in steps] != [e[0] for e in entries]:
        return 'migration stack undetermined: incomplete', set()
    for step in steps:
        if step.outcome not in ('clean', 'rechained'):
            return f'{step.spec}: {step.outcome}' + (f' ({step.reason})' if step.reason else ''), set()
    moved = {s.spec for s in steps if s.outcome == 'rechained'}
    if moved and not enabled():
        return 'bookkeeping conflict needs re-chain, which is disabled: ' + ', '.join(sorted(moved)), set()
    return None, moved


class Evidence(list):
    """An `ev` stand-in that keeps the resolver's events and writes nothing."""

    def __bool__(self):
        return True  # callers choose `emit or ev`; an empty collector must still win

    def __call__(self, kind, subject, **fields):
        self.append(dict(fields, type=kind, subject=subject))


def parents(repo, base, cand, window, kept, events):
    """-> (rechained_specs in member order, {spec: {old_parent, new_parent}}).

    Only resolver evidence for this window whose merge commit is on the final
    candidate's first-parent chain counts: stale candidates, dropped members and
    other windows' events cannot appear.
    """
    try:
        chain = set(rechain.out(repo, 'rev-list', '--first-parent', f'{base}..{cand}').splitlines())
    except Exception:
        return [], {}
    found = {}
    for e in events:
        if (e.get('type') == 'rechained' and e.get('window') == window and e.get('subject') in kept
                and e.get('merge_sha') in chain and isinstance(e.get('old_parent'), str)
                and isinstance(e.get('new_parent'), str)):
            found[e['subject']] = {'old_parent': e['old_parent'], 'new_parent': e['new_parent']}
    names = [s for s in kept if s in found]
    return names, {s: found[s] for s in names}


def rechained_specs(repo, base, cand, live):
    """Subjects whose merge on the final first-parent chain is a mechanical re-chain."""
    moved = set()
    for sha in rechain.out(repo, 'rev-list', '--first-parent', f'{base}..{cand}').splitlines():
        message = rechain.out(repo, 'show', '-s', '--format=%s', sha)
        if message.endswith('(mechanical re-chain)'):
            moved.update(s for s in live if message.startswith(f'merge({s}):'))
    return moved


def batch_record(repo, old, new, window, kept):
    """The shipped fields shared by every member of the surviving batch."""
    import fold
    try:
        events = fold.read_events(use_cache=False)
    except Exception:
        events = []
    names, mapping = parents(repo, old, new, window, kept, events)
    return {'rechained_specs': names, 'rechain': mapping}


def receipt_fields(record):
    """Validated receipt fields; anything malformed is an empty record."""
    names, mapping = record.get('rechained_specs'), record.get('rechain')
    if (isinstance(names, list) and all(isinstance(s, str) for s in names) and isinstance(mapping, dict)
            and set(mapping) == set(names)
            and all(isinstance(v, dict) and set(v) == {'old_parent', 'new_parent'}
                    and all(isinstance(x, str) for x in v.values()) for v in mapping.values())):
        return {'rechained_specs': list(names), 'rechain': {s: dict(mapping[s]) for s in names}}
    return {'rechained_specs': [], 'rechain': {}}


def bookkeeping_overlap(repo, base, kept, entry, owners):
    """True when `entry` and every overlapping owner add migrations and the
    cumulative stack (resolver-proven, re-chain enabled or not needed) admits all."""
    by_spec = {e[0]: e for e in kept}
    if not all(o in by_spec and adds_migration(repo, base, by_spec[o][2]) for o in owners):
        return False
    if not adds_migration(repo, base, entry[2]):
        return False
    reason, _ = stack_check(repo, base, list(kept) + [entry])
    return reason is None
