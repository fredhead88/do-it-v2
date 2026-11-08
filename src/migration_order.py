#!/usr/bin/env python3
"""Read-only prerequisite ordering for a caller-supplied ready set.

Holds determine chain weight; holds plus explicit/exact Interfaces determine
ordering. No readiness, admission, or merge verdict is implied by this result.
"""
import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import sys

import autodispatch
import dependency_holds
import fold
import ledger_files
import merge_gate
import project_scope


@dataclass
class OrderResult:
    ordered: list[str]
    deferred: dict[str, str]
    cycles: list[list[str]]


def _terminal(events, kind):
    return {e.get('subject') for e in events if e.get('type') == kind}


def _unfenced(text):
    fence = None
    for line in text.splitlines():
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            continue
        if fence is None:
            yield line


def _markup(value):
    return re.sub(r'\s+', ' ', value.strip().strip('`\'"*_ ')).strip()


def _signature(value):
    span = re.search(r'(`+)(.*?)\1', value, re.S)
    if span:
        return _markup(span[2])
    # Legacy unquoted declarations end at the return type, or prose delimiter.
    value = re.split(r';|\s+\(fields\b', value, maxsplit=1)[0]
    head, arrow, annotation = value.partition('->')
    if not arrow:
        return _markup(value)
    annotation = annotation.strip()
    depth, end = 0, len(annotation)
    for index, char in enumerate(annotation):
        if char in '[({':
            depth += 1
        elif char in '])}':
            depth -= 1
        elif char.isspace() and depth == 0:
            before, after = annotation[:index].rstrip(), annotation[index:].lstrip()
            if not (before.endswith('|') or after.startswith(('|', '['))):
                end = index
                break
    return _markup(head + arrow + ' ' + annotation[:end])


def _module(signature):
    qualified = signature.split('(', 1)[0].strip()
    if '::' in qualified:
        module = qualified.rsplit('::', 1)[0]
        return module.rsplit('/', 1)[-1].removesuffix('.py')
    return qualified.rsplit('.', 1)[0] if '.' in qualified else ''


def _interfaces(text):
    entries = {'consumes': [], 'produces': []}
    inside, label = False, None
    for raw in _unfenced(text):
        heading = re.match(r'^\s*#+\s+(.*)', raw)
        if heading:
            inside = _markup(heading[1]).lower() == 'interfaces'
            label = None
            continue
        if not inside:
            continue
        line = raw.strip()
        match = re.match(r'^(?:\*\*)?(Consumes|Produces)(?:\*\*)?:(?:\*\*)?\s*(.*)$', line, re.I)
        if match:
            label = match[1].lower()
            if match[2]:
                entries[label].append(match[2])
            continue
        if re.match(r'^[\w *-]+:', line):
            label = None
        if label and line:
            bullet = re.match(r'^[-*+]\s+(.*)', line)
            if bullet:
                entries[label].append(bullet[1])
            elif raw[:1].isspace() and entries[label]:
                entries[label][-1] += ' ' + line
            else:
                entries[label].append(line)
    return {key: [(' '.join(sorted(set(re.findall(r'\bL-spec-[0-9]+\b', value))))
                   if key == 'consumes' and re.search(r'\bL-spec-[0-9]+\b', value)
                   else _signature(value)) for value in values if _markup(value)]
            for key, values in entries.items()}


def _snapshot(events):
    own = defaultdict(list)
    for event in events:
        own[event.get('subject')].append(event)
    retracted = {sid for sid, rows in own.items()
                 if any(e.get('type') == 'charter-retracted' for e in rows)}
    shipped = _terminal(events, 'shipped')
    closed = shipped | {sid for sid, rows in own.items()
                        if dependency_holds.valid_dependency(sid)
                        and fold.spec_state(rows, retracted) in
                        ('killed', 'void', 'dropped', 'closed-unbuilt', 'closed-shipped')}
    return own, shipped, closed


def _metadata(texts, events):
    """Validate all potential waiters/providers before computing any weight."""
    own, shipped, closed = _snapshot(events)
    if any(not dependency_holds.valid_dependency(sid) for sid in texts):
        return {}, {}, {'project': 'invalid spec id'}
    metadata, holds, invalid = {}, {}, {}
    for sid in sorted(texts):
        if sid in closed:
            continue
        if not dependency_holds.valid_dependency(sid) or not isinstance(texts[sid], str):
            invalid[sid] = 'metadata unavailable'
            continue
        try:
            text = texts[sid]
            dependency = dependency_holds.from_spec_text(text)
            merge_gate.grant_from_text(text)
            metadata[sid] = _interfaces(text)
            rows = own[sid]
            written = max((e for e in rows if e.get('type') in ('spec-written', 'spec-amended')),
                          key=autodispatch._hold_order, default=None)
            ledger_holds = [e for e in rows if e.get('type') == 'dependency-hold' and
                            (written is None or fold.ts(e.get('ts')) >= fold.ts(written.get('ts')))]
            hold = max(ledger_holds, key=autodispatch._hold_order, default=None)
            if hold is None and dependency:
                hold = dict(written or {'ts': None},
                            type='dependency-hold', subject=sid, waits_for=dependency,
                            why='Dependency-hold declared in spec text',
                            _src='spec-text:' + sid)
            if hold is not None:
                dependency = hold.get('waits_for')
                if not dependency_holds.valid_dependency(dependency):
                    raise ValueError('invalid dependency-hold')
                if (not autodispatch._hold_released(hold, rows)
                        and autodispatch.held_by_dependency([hold], events, shipped=shipped)):
                    holds[sid] = dependency
        except (ValueError, TypeError, merge_gate.Undetermined):
            # Never echo raw metadata: errors must be single protocol-safe lines.
            invalid[sid] = 'invalid dependency or Writes metadata'
    return metadata, holds, invalid


def _reverse_holds(holds):
    reverse = defaultdict(set)
    for waiter, prerequisite in holds.items():
        reverse[prerequisite].add(waiter)
    return reverse


def _weight(spec, reverse):
    seen, pending = {spec}, [spec]
    while pending:
        for waiter in reverse[pending.pop()] - seen:
            seen.add(waiter)
            pending.append(waiter)
    return len(seen)


def chain_weight(spec, events, open_specs):
    """One plus unique open transitive hold waiters; never count the root twice."""
    if not dependency_holds.valid_dependency(spec):
        raise ValueError('invalid spec id')
    events = list(events)
    _, holds, invalid = _metadata(open_specs, events)
    if invalid:
        raise ValueError('undetermined metadata for ' + ', '.join(sorted(invalid)))
    return _weight(spec, _reverse_holds(holds))


def _cycles(graph):
    """Iterative Kosaraju SCCs avoid recursion limits on long project chains."""
    seen, finish = set(), []
    for root in sorted(graph):
        if root in seen:
            continue
        seen.add(root)
        stack = [(root, iter(sorted(graph[root])))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                finish.append(node)
                stack.pop()
            elif child not in seen:
                seen.add(child)
                stack.append((child, iter(sorted(graph[child]))))
    reverse = {node: set() for node in graph}
    for node, children in graph.items():
        for child in children:
            reverse[child].add(node)
    seen, cycles = set(), []
    for root in reversed(finish):
        if root in seen:
            continue
        component, stack = [], [root]
        seen.add(root)
        while stack:
            node = stack.pop()
            component.append(node)
            for child in reverse[node] - seen:
                seen.add(child)
                stack.append(child)
        if len(component) > 1 or root in graph[root]:
            cycles.append(sorted(component))
    return sorted(cycles)


def order(specs, events, *, texts):
    """Order requested ids by prerequisites, weight, latest clean grade, then id."""
    requested = set(specs)
    if any(not dependency_holds.valid_dependency(sid) for sid in requested):
        raise ValueError('invalid requested spec id')
    if not requested:
        return OrderResult([], {}, [])
    events = list(events)
    metadata, holds, invalid = _metadata(texts, events)
    if invalid:
        blocker = sorted(invalid)[0]
        return OrderResult([], {sid: f'undetermined {invalid[blocker]} for {blocker}'
                                for sid in sorted(requested)}, [])
    _, shipped, closed = _snapshot(events)
    killed = _terminal(events, 'spec-killed')
    providers = defaultdict(set)
    modules = set()
    for sid, declarations in metadata.items():
        for signature in declarations['produces']:
            providers[signature].add(sid)
            modules.add(_module(signature))
    graph, deferred = {sid: set() for sid in requested}, {}
    for sid in sorted(requested):
        if sid in closed:
            deferred[sid] = f'requested spec {sid} is terminal'
            continue
        if sid not in texts or not isinstance(texts[sid], str):
            deferred[sid] = f'metadata unavailable for {sid}'
            continue
        dependencies = {holds[sid]} if sid in holds else set()
        for signature in metadata.get(sid, {}).get('consumes', []):
            explicit = set(re.findall(r'\bL-spec-[0-9]+\b', signature))
            if explicit:
                dependencies.update(explicit)
            elif len(providers[signature]) > 1:
                deferred[sid] = 'ambiguous interface providers: ' + ' '.join(sorted(providers[signature]))
            elif not providers[signature] and _module(signature) and _module(signature) in modules:
                deferred[sid] = (f'undetermined interface in module {_module(signature)}: {signature}')
            else:
                dependencies.update(providers[signature])
        for dependency in sorted(dependencies):
            if dependency in killed:
                deferred.setdefault(sid, f'prerequisite {dependency} is killed')
            elif dependency in shipped:
                continue
            elif dependency in closed:
                deferred.setdefault(sid, f'prerequisite {dependency} is closed and unshipped')
            elif dependency not in requested:
                deferred.setdefault(sid, f'prerequisite {dependency} is not requested or shipped')
            else:
                graph[sid].add(dependency)
    cycles = _cycles(graph)
    for component in cycles:
        for sid in component:
            deferred[sid] = 'cycle: ' + ' '.join(component)
    # Propagate from a snapshot each round: stable reasons do not depend on traversal.
    while True:
        added = {sid: f'blocked by {dep}: {deferred[dep]}'
                 for sid in sorted(requested - deferred.keys())
                 for dep in [next((d for d in sorted(graph[sid]) if d in deferred), None)] if dep}
        if not added:
            break
        deferred.update(added)
    grades = {}
    for sid in requested:
        spec_evs = [e for e in events if e.get('subject') == sid]
        owed = fold.verdict_owed_criteria(spec_evs, sid)
        qualifying = [fold.ts(e.get('ts')) for e in events if e.get('subject') == sid
                      and e.get('type') == 'verdict' and fold.verdict_confirmed(e, owed)]
        if qualifying:
            grades[sid] = max(qualifying)
    reverse = _reverse_holds(holds)
    weights = {sid: _weight(sid, reverse) for sid in requested}
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    pending, ordered = requested - deferred.keys(), []
    while pending:
        eligible = [sid for sid in pending if not graph[sid].intersection(pending)]
        chosen = min(eligible, key=lambda sid: (-weights[sid], grades.get(sid, far_future), sid))
        ordered.append(chosen)
        pending.remove(chosen)
    return OrderResult(ordered, dict(sorted(deferred.items())), cycles)


def _project_evidence(project, events):
    """Scope the reader's corrected snapshot and acquire only spec metadata."""
    if not project_scope.registered(fold.ROOT, project):
        raise ValueError('unknown project')
    if not fold.EVENTS.is_dir() or ledger_files.problems(fold.EVENTS):
        raise ValueError('ledger unavailable or corrupt')
    for row in events:
        if not isinstance(row, dict) or not all(isinstance(row.get(key), str)
                                                for key in ('type', 'subject', 'ts')):
            raise ValueError('corrupt ledger event')
        datetime.fromisoformat(row['ts'].replace('Z', '+00:00'))
    subjects = {e['subject'] for e in events if dependency_holds.valid_dependency(e.get('subject'))}
    selected = {sid for sid in subjects if project_scope.subject_project(events, sid) == project}
    charters = {fold.charter_id(e.get('charter')) for e in events if e.get('subject') in selected}
    events = [e for e in events if e.get('subject') in selected | charters]
    _, _, closed = _snapshot(events)
    texts = {}
    for sid in sorted(selected - closed):
        try:
            texts[sid] = (fold.ROOT / 'content' / f'{sid}.md').read_text()
        except (OSError, UnicodeError) as exc:
            raise ValueError(f'project metadata unavailable for {sid}') from exc
    return events, texts


def cli(argv, events):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--specs', required=True)
    args = parser.parse_args(argv)
    try:
        ids = args.specs.split(',')
        if any(not dependency_holds.valid_dependency(sid) for sid in ids):
            raise ValueError('invalid requested spec id')
        events, texts = _project_evidence(args.project, events)
        known = {e.get('subject') for e in events}
        if any(sid not in known for sid in ids):
            raise ValueError('requested metadata unavailable')
        result = order(ids, events, texts=texts)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        print('migration-order: undetermined project evidence or arguments', file=sys.stderr)
        return 2
    if any(reason.startswith('undetermined invalid') or
           reason.startswith('undetermined metadata') for reason in result.deferred.values()):
        print('migration-order: undetermined project metadata', file=sys.stderr)
        return 2
    for sid in result.ordered:
        print(sid)
    for sid, reason in result.deferred.items():
        print('DEFERRED', sid, reason)
    for component in result.cycles:
        print('CYCLE', *component)
    return 2 if result.cycles else 0
