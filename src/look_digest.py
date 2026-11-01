"""Daily look digests. The ledger announces once; the linked JSON keeps filling.

All callers hold look.lock. A pass buffers its announcements so each ledger
reading includes every affected key seen during that pass. Later passes update
that day's JSON, never append another brief for the same condition.
"""
import json
from datetime import timezone

CAP = 50


class Digest:
    def __init__(self, root, events, now, *, dry_run=False):
        self.root, self.now, self.dry_run = root, now, dry_run
        self.events = events
        self.day = now.astimezone(timezone.utc).date().isoformat()
        self.path = root / 'look' / 'digests' / (self.day + '.json')
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = dict(day=self.day, emitted=0, suppressed=0, conditions={})
            # Reconstruct dedupe/cap after state loss, including pre-digest rows.
            for e in events:
                if e.get('type') != 'brief' or not str(e.get('ts', '')).startswith(self.day):
                    continue
                if not (e.get('condition') or str(e.get('subject', '')).startswith('look:')):
                    continue
                condition = e.get('condition') or e['subject'].split(':')[1]
                self.data['emitted'] += 1
                row = self.data['conditions'].setdefault(condition, dict(items={}, suppressed=0))
                row['ref'] = e.get('_src', 'ledger')
        self.pending = {}
        self.seen = set()

    def add(self, condition, key, owner, reading, problem=None):
        self.seen.add((condition, key))
        row = self.data['conditions'].setdefault(condition, dict(items={}, suppressed=0))
        item = dict(key=key, owner=owner, reading=reading)
        row['items'][key] = item
        if condition in self.pending:
            self.pending[condition]['items'] = list(row['items'].values())
            row['suppressed'] += 1
            self.data['suppressed'] += 1
            return None
        if row.get('ref') or self.data['emitted'] >= CAP:
            row['suppressed'] += 1
            self.data['suppressed'] += 1
            return None
        self.data['emitted'] += 1
        brief = dict(type='brief', condition=condition, key=key, owner=owner,
                     problem=problem or 'look-' + condition, reading=reading,
                     subject=f'look:{condition}:{self.day}',
                     measured_at=self.now.isoformat(timespec='seconds'),
                     digest_path=str(self.path), items=list(row['items'].values()))
        self.pending[condition] = brief
        return brief

    def finish(self, emit, mark):
        for condition, brief in self.pending.items():
            keys = [i['key'] for i in brief['items']]
            brief['why'] = f"daily {condition}: {', '.join(keys)}; digest={self.path}; first reading={brief['reading']}"
            ref = 'dry-run' if self.dry_run else emit(brief)
            if ref is None:
                raise RuntimeError('look digest brief append refused')
            brief['_src'] = ref
            self.data['conditions'][condition]['ref'] = ref
            if not self.dry_run:
                for key in keys:
                    mark(f'{condition}|{key}', ref)
        if not self.dry_run:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.data, sort_keys=True))
            tmp.replace(self.path)
