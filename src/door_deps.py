"""Ledger Door dependency vocabulary and conservative event routing.

This is the table/classifier slice of L-spec-2222. It is not a completeness
certificate: the source-read guard and executable mutation-seed matrix have not
yet been implemented. No production reader consumes this module yet. A caller
must use a full read on ``unknown`` and must separately establish completeness
and freshness of every non-ledger input.

Routes are retrieval roles, never a subject match: classify has no target-id
argument. ``own`` requires the caller to have selected the target subject.
Overlapping route declarations are retained; the single returned route follows
global > ref > spawn > sibling > mention > own. Same-ts dependencies include
cross-file content ordering and append order within a file, not a seventh route.
"""
from dataclasses import dataclass
import re
from typing import Literal

import rulings
from dependency_holds import valid_dependency


ACTION_CLASSES = (
    'next-step', 'build-start', 'grade', 'review', 'merge', 'close',
    'hold-release', 'capability-release', 'wave-order', 'charter-close',
)


@dataclass(frozen=True)
class Deps:
    own_kinds: tuple[str, ...]
    mention_kinds: tuple[str, ...]
    global_kinds: tuple[str, ...]
    ref_kinds: tuple[str, ...]
    spawn_kinds: tuple[str, ...]
    ledger_wide_sets: tuple[str, ...]
    membership: tuple[str, ...]
    same_ts_neighbours: bool
    answer: str
    non_ledger_inputs: tuple[str, ...] = ()


# Selectors include existence, edits, creation and removal, not only contents of
# files that currently exist. They are NOT classifier event kinds. The directory
# listing is sorted before membership evaluation. Resolved charter means the
# production dispatch.resolve_charter result, not the last event's charter.
NON_LEDGER_INPUTS = {
    'content:spec-text': (
        'content/<item>.md; Dependency-hold waits_for',
        ('dependency_holds.text_hold_reason', 'dispatch.spec_path')),
    'content:spec-membership': (
        'sorted content/*.md directory listing and spec text',
        ('fold.held_specs',)),
    'content:grader-script': (
        'content/verify-<spec>-grader.sh', ('fold.held_specs',)),
    'content:plan': (
        'content/plan-<resolved-charter>.md',
        ('dispatch.resolve_charter', 'dispatch.spec_wave', 'dispatch._plan_units')),
    'repo:lane-rules': ('lane_rules.DEFAULT_TABLE', ('lane_rules.load',)),
}

# Item-local inputs to the *complete* Step: action eligibility, hold/clears,
# history, retry, owed observation and reason are one answer, even for a caller
# interested only in whether a particular action can start.
_OWN = (
    'spec-written', 'spec-amended', 'spec-carried', 'spec-killed', 'spec-closed',
    'kill-accepted', 'rejected-criterion', 'criterion-cleared', 'must-fix',
    'build-started', 'build-done', 'build-blocked', 'build-deviation',
    'build-stub', 'verdict', 'cannot-assess', 'review',
    'question', 'escalation-blocking', 'decision', 'unblocked',
    'dependency-hold', 'shipped', 'spawn-started', 'spawn-done',
    'spawn-failed', 'spawn-stale', 'autodispatched', 'autodispatch-failed',
    'autodispatch-skipped',
    'merge-gate-clean', 'merge-gate-rework', 'merge-gate-undetermined',
    'merge-conflict', 'queue-add', 'queue-remove', 'queue-batch',
    'window-dropped', 'window-opened', 'window-redcut', 'window-merged',
    'window-done', 'window-verified', 'window-cancelled', 'member-verdict',
    'window-default',
    'owed-ac', 'owed-met', 'owed-failed', 'owed-corrective',
    'owed-waived', 'owed-unverifiable', 'owed-routed', 'owed-quarantined',
    'criterion-routed',
)

# Sources of the concrete ledger-wide derived sets below. Queue membership and
# terminal outcomes can change owner selection even for a different subject.
_QUEUE = (
    'queue-add', 'queue-remove', 'queue-batch', 'window-redcut',
    'shipped', 'window-dropped', 'merge-conflict', 'spec-killed', 'spec-closed',
)
_GLOBAL = ('correction', 'decision', 'unblocked', 'capability-hold',
           'charter-retracted') + _QUEUE
_SPAWN = ('spawn-started', 'build-started', 'spawn-done', 'spawn-failed',
          'spawn-stale', 'seat-claimed')
_MEMBERSHIP = ('spec-written', 'spec-carried')

# The union is intentional across all seven consumers of step_for. Joining
# fields are detailed in RELATIONS rather than implied by a kind's spelling.
_ITEM = Deps(
    own_kinds=_OWN,
    mention_kinds=('dependency-hold',) + _MEMBERSHIP,
    global_kinds=_GLOBAL,
    ref_kinds=('decision', 'unblocked'),
    spawn_kinds=_SPAWN,
    ledger_wide_sets=('shipped', 'retracted', 'capability_holds',
                      'terminal_spawns', 'merge_queue'),
    membership=('charter', 'wave'),
    same_ts_neighbours=True,
    answer='nextstep.step_for',
    non_ledger_inputs=tuple(NON_LEDGER_INPUTS),
)

TABLE: dict[str, Deps] = {action: _ITEM for action in ACTION_CLASSES[:7]}
TABLE['capability-release'] = Deps(
    own_kinds=(), mention_kinds=(),
    global_kinds=('correction', 'capability-hold', 'decision', 'unblocked'),
    ref_kinds=(), spawn_kinds=(), ledger_wide_sets=('capability_holds',),
    membership=(), same_ts_neighbours=True, answer='fold.capability_holds')
TABLE['wave-order'] = Deps(
    own_kinds=_OWN, mention_kinds=_MEMBERSHIP,
    global_kinds=('correction', 'charter-retracted'),
    ref_kinds=(), spawn_kinds=(), ledger_wide_sets=('retracted',),
    membership=('charter', 'wave'), same_ts_neighbours=True,
    answer='nextstep._wave', non_ledger_inputs=('content:plan',))
TABLE['charter-close'] = Deps(
    own_kinds=_OWN + ('charter-filed', 'charter-review-complete', 'charter-retracted',
                     'l1-complete', 'sweep-fixpoint', 'brief', 'brief-answered',
                     'charter-proving', 'charter-reopened', 'charter-closed'),
    mention_kinds=_MEMBERSHIP,
    global_kinds=('correction', 'charter-retracted'),
    ref_kinds=(), spawn_kinds=(), ledger_wide_sets=('retracted',),
    membership=('charter',), same_ts_neighbours=True, answer='fold.closable')

# Source symbols checked by ContractTests. These citations are not a waiver for
# new reads and do not replace the still-missing read-level AST inventory.
_ITEM_SOURCES = (
    'fold.finish_events', 'fold.apply_corrections', 'fold.fold',
    'fold.answered', 'fold.capability_holds', 'fold.terminal_spawns',
    'fold.held_specs', 'nextstep.next_steps', 'nextstep._unanswered',
    'nextstep._wave', 'dependency_holds.text_hold_reason',
    'autodispatch.held_by_dependency', 'merge_queue.queue',
    'tick._steps', 'dispatch.resolve_charter', 'dispatch.spec_wave',
    'rulings.project_event',
)
SOURCE_SYMBOLS = {action: _ITEM_SOURCES for action in ACTION_CLASSES[:7]}
SOURCE_SYMBOLS.update({
    'capability-release': ('fold.capability_holds', 'fold.finish_events'),
    'wave-order': ('nextstep._wave', 'tick._steps', 'dispatch.resolve_charter',
                   'dispatch.spec_wave', 'fold.fold', 'fold.finish_events'),
    'charter-close': ('fold.closable', 'fold._closable', 'fold.fold',
                      'proving.unproven_checks', 'fold.finish_events'),
})

RELATIONS = {
    'correction': 'ref -> _src; set can change type, subject, charter, footprint, project',
    'ruling': 'decision.ruling.spec/window/nodes; rulings.project_event before subject selection',
    'ref': 'decision/unblocked.ref -> question/escalation-blocking._src; subject need not match',
    'spawn': 'spawn -> spawn; seat-claimed.subject -> spawn; terminal may have no subject',
    'shipped': 'shipped.subject -> dependency-hold.waits_for (also spec-text waits_for)',
    'retracted': 'charter-retracted.subject -> normalized charter field',
    'capability_holds': 'capability:* subject, spec and capability; decision/unblocked same subject',
    'terminal_spawns': 'spawn-done/spawn-failed/spawn-stale.spawn, not subject',
    'merge_queue': 'queue-add/remove.subject; queue-batch.specs and project; '
                   'window-redcut.window/returned/project; terminal subjects; stable ts order',
    'charter': 'spec event charter; fold.fold newest charter, dispatch.resolve_charter '
               'newest spec-written/spec-carried charter; correction can change membership',
    'wave': 'spec-written.footprint + dispatch.resolve_charter + content:plan; '
            'correction can change footprint/charter; sibling state and terminal event kinds',
    'same-ts': 'fold._merge_same_ts cross-file content ordering and within-file source order',
}

Role = Literal['own', 'mention', 'global', 'ref', 'spawn', 'sibling', 'unknown']


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def classify(event: dict, action_class: str) -> Role:
    """Return a declared retrieval role, or unknown for an unsupported shape.

    This validates dependency selectors, not event admission: it does not grant
    permission to append or act on an event. Callers retain fold's admission,
    normalization and full-read fallback. Actor/source/time validation belongs
    to those operations, not this selector.
    """
    if not isinstance(action_class, str) or action_class not in TABLE:
        return 'unknown'
    if not isinstance(event, dict) or not isinstance(event.get('type'), str):
        return 'unknown'
    row, kind = TABLE[action_class], event['type']
    declared = (row.own_kinds + row.mention_kinds + row.global_kinds
                + row.ref_kinds + row.spawn_kinds)
    if kind not in declared:
        return 'unknown'
    # Do not interpret malformed selectors as an item-local event.
    if 'subject' in event and not isinstance(event['subject'], str):
        return 'unknown'
    for key in ('ref', 'spawn', 'charter', 'capability', 'spec', 'branch', 'window'):
        if key in event and not _text(event[key]):
            return 'unknown'
    if 'waits_for' in event and not valid_dependency(event['waits_for']):
        return 'unknown'
    if 'specs' in event and (not _text(event['specs']) or not all(
            valid_dependency(s) for s in event['specs'].split(','))):
        return 'unknown'
    if 'returned' in event and (not isinstance(event['returned'], list)
                               or not all(valid_dependency(s) for s in event['returned'])):
        return 'unknown'
    if kind == 'dependency-hold' and 'waits_for' not in event:
        return 'unknown'
    if kind == 'queue-add' and 'branch' not in event:
        return 'unknown'
    if kind == 'queue-batch' and 'specs' not in event:
        return 'unknown'
    if kind == 'window-redcut' and not {'window', 'returned'} <= event.keys():
        return 'unknown'
    if 'footprint' in event and (not isinstance(event['footprint'], list)
                                or not event['footprint']
                                or not all(_text(p) for p in event['footprint'])):
        return 'unknown'
    if kind == 'correction':
        changes = event.get('set')
        if (not _text(event.get('ref')) or not isinstance(changes, dict)
                or not changes or not all(_text(k) for k in changes)
                or {'actor', '_src'} & changes.keys()):
            return 'unknown'
        return 'global' if kind in row.global_kinds else 'unknown'
    if 'ruling' in event:
        if kind != 'decision' or event.get('actor') not in ('thinker', 'operator'):
            return 'unknown'
        try:
            rulings.validate(event['ruling'])
        except (ValueError, TypeError):
            return 'unknown'
        return 'global' if kind in row.global_kinds else 'unknown'
    subject = event.get('subject')
    capability = isinstance(subject, str) and subject.startswith('capability:')
    if kind == 'capability-hold' or capability:
        if not capability or not _text(subject[len('capability:'):]):
            return 'unknown'
        return 'global' if kind in row.global_kinds else 'unknown'
    if kind in row.global_kinds and kind not in ('decision', 'unblocked'):
        return 'global' if _text(subject) else 'unknown'
    if 'ref' in event:
        return 'ref' if kind in row.ref_kinds else 'unknown'
    if kind in row.spawn_kinds:
        if kind == 'seat-claimed':
            return 'spawn' if _text(event.get('spawn')) or (
                _text(subject) and re.fullmatch(r'L-(?!spec-|charter-)[a-z][a-z0-9-]*-[0-9]+', subject)) else 'unknown'
        return 'spawn' if _text(event.get('spawn')) else 'unknown'
    if 'spawn' in event:
        return 'unknown'
    # fold.fold/spec_state resolve membership from any admitted own event's
    # charter. The production wave resolver additionally reads the footprint
    # of spec-written (and charter from spec-written/spec-carried).
    if row.membership and ('charter' in event or (
            kind in _MEMBERSHIP and 'footprint' in event)):
        return 'sibling' if _text(subject) else 'unknown'
    if kind in row.mention_kinds and 'waits_for' in event:
        return 'mention' if _text(subject) else 'unknown'
    if kind in row.own_kinds and _text(subject):
        return 'own'
    return 'unknown'
