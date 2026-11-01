"""Per-pass item failures and bounded, durable three-tick escalation state."""
import json
import ledger_files
import os
import sys
from pathlib import Path
import tempfile


class Pass:
    def __init__(self, events, now):
        import fold
        import tick
        self.events, self.now = events, now
        self.dst = tick.tick_path()
        self.state_path = fold.ROOT / 'state/autodispatch-item-failures.json'
        self.failed = {}
        self.baseline = len(self._rows())

    def _rows(self):
        try:
            rows = []
            for line in ledger_files.read_text(self.dst.parent, self.dst.name).splitlines():
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
            return rows
        except FileNotFoundError:
            return []

    def fail(self, item, error, *, phase):
        import dispatch
        # This signal already owns a bounded retry counter. Recording a
        # standing failure would prevent its next tick from healing the tree.
        heal = sys.modules.get('worktree_heal')
        if heal is not None and isinstance(error, getattr(heal, 'RepairRetry', ())):
            return
        if item in self.failed:
            return
        # Preserve exception type without copying an untrusted event payload.
        reason = error if isinstance(error, str) else type(error).__name__
        self.failed[item] = dict(reason=reason[:300], phase=phase)
        if not any(e.get('type') == 'autodispatch-failed' and e.get('subject') == item
                   for e in self._rows()[self.baseline:]):
            self.dst.parent.mkdir(parents=True, exist_ok=True)
            refused = dispatch.emit(self.dst, {'subject': item}, 'autodispatch-failed', strict=True,
                reason=reason[:300], failure_scope='item', phase=phase, tick_at=self.now.isoformat())
            if refused:
                raise ValueError('item failure record refused: ' + str(refused))

    def finish(self):
        import dispatch
        try:
            previous = json.loads(self.state_path.read_text())
        except (FileNotFoundError, ValueError):
            previous = {}
        previous = previous if isinstance(previous, dict) else {}
        state = {}
        for item, diagnostic in self.failed.items():
            prior = previous.get(item, {})
            prior = prior if isinstance(prior, dict) else {}
            same_tick = prior.get('last_tick') == self.now.isoformat()
            old_count = prior.get('count', 0)
            count = (old_count if type(old_count) is int and old_count >= 0 else 0) + (not same_tick)
            raised = prior.get('raised', False)
            if count >= 3 and not raised:
                refusal = dispatch.emit(self.dst, {'subject': item}, 'escalation-blocking', strict=True,
                    kind='autodispatch-item-repeat', owner='executor', count=count,
                    why='Automatic item failed on three consecutive ticks: ' + diagnostic['reason'],
                    default='Keep this item held; continue dispatching other items',
                    deadline=self.now.isoformat(), revert='Repair item input and allow a successful tick')
                if refusal:
                    raise ValueError('item escalation refused: ' + str(refusal))
                raised = True
            state[item] = dict(count=count, raised=raised, last_tick=self.now.isoformat())
        # Absence in this pass resets the streak, without emitting healthy-item noise.
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='autodispatch-items-', dir=self.state_path.parent)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(state, handle)
            os.replace(name, self.state_path)
        finally:
            if Path(name).exists():
                Path(name).unlink()
