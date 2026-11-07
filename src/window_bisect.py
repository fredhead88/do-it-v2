"""Bounded diagnostic disposition of an admitted, queue-owned red rehearsal.

Diagnostic CI never authorizes a merge. Unknown work is durable retry work.
The external lane must acknowledge the exact-group reader and shared lock.
"""
import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import base_red
import fold
import window_attribution as attribution
import window_gate as gate
import window_members
import window_rehearse


SHA = re.compile(r'[0-9a-f]{40}')
SPEC = re.compile(r'L-spec-[0-9]{4,}')
REHEARSAL = re.compile(r'rh-[A-Za-z0-9][A-Za-z0-9_-]{0,100}')
PROJECT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}')
HEX = re.compile(r'[0-9a-f]{64}')
LIMIT = 64000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def regular(path, *, absent=False):
    """Reject symlinks in every component, including still-absent outputs."""
    path = Path(os.path.abspath(path))
    for item in [*reversed(path.parents), path]:
        require(not item.is_symlink(), 'symlink lane path refused')
    require(not path.exists() or path.is_file(), 'non-regular lane file')
    require(absent or path.is_file(), 'required lane file missing')
    return path


def directory(path):
    path = Path(os.path.abspath(path))
    require(all(not p.is_symlink() for p in [path, *path.parents]), 'symlink lane directory')
    require(not path.exists() or path.is_dir(), 'invalid lane directory')
    return path


def read_json(path, *, limit=1024 * 1024):
    path = regular(path)
    require(path.stat().st_size <= limit, 'oversized JSON input')
    return json.loads(path.read_text(encoding='utf-8'))


def ids_list(value):
    require(isinstance(value, list) and len(value) <= 1000, 'invalid failing-id list')
    require(all(isinstance(n, str) and 0 < len(n) <= 4096
                and not any(ord(c) < 32 or ord(c) == 127 for c in n) for n in value),
            'invalid failing-id string')
    require(all(not n.startswith(('-', '/')) and '..' not in Path(n.split('::', 1)[0]).parts
                for n in value), 'unsafe failing-id selector')
    require(len(set(value)) == len(value), 'duplicate failing ids')
    return value


def members_list(value):
    require(isinstance(value, list) and 0 < len(value) <= 128
            and all(isinstance(s, str) and SPEC.fullmatch(s)
                    and s == f'L-spec-{int(s[7:]):04d}' for s in value), 'invalid spec list')
    require(len(set(value)) == len(value)
            and len({int(s[7:]) for s in value}) == len(value), 'duplicate specs')
    return value


def admitted_events():
    return [e for e in gate.train_events()
            if fold.admits(e, e.get('actor', '')) is None]


def lineage(project, root, members):
    return hashlib.sha256(json.dumps([project, root, members], separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


def atomic(path, content):
    path = regular(path, absent=True)
    fd, name = tempfile.mkstemp(prefix='.bisect-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save(path, value):
    atomic(path, (json.dumps(value, sort_keys=True) + '\n').encode())


@contextmanager
def lane_lock(queue):
    path = regular(queue / '.bisect.lock', absent=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def protocol(queue):
    value = read_json(queue / 'bisect-protocol.json')
    require(isinstance(value, dict) and type(value.get('version')) is int
            and value['version'] == 1 and value.get('retry_reader') is True
            and value.get('shared_writers_lock') is True, 'lane protocol acknowledgement missing')


@contextmanager
def diagnostics():
    """Keep inherited helpers' output off stdout and redact it before display."""
    from prod_runner import redact
    with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as captured:
        try:
            with redirect_stdout(captured), redirect_stderr(captured):
                yield
        finally:
            captured.seek(0)
            for line in captured:
                print(redact(line.rstrip('\n')), file=sys.stderr)


def new_failures(project, base, ids):
    """Return only established new ids; None means the exact base is unknown."""
    try:
        selected = base_red.classify_selected(project, base, ids)
        known = ids_list(selected['known_red'])
        pending = ids_list(selected['rerun_on_base'])
        require(not set(known) & set(pending) and set(known + pending) == set(ids),
                'incomplete base classification')
        if not pending:
            return []
        result = base_red.rerun_on_base(project, base, pending)
        old, new = ids_list(result['pre_existing']), ids_list(result['new'])
        require(result.get('undetermined') is False and not set(old) & set(new)
                and set(old + new) == set(pending), 'unresolved base evidence')
        return [n for n in ids if n in new]
    except (KeyError, TypeError, ValueError, OSError, RuntimeError, subprocess.SubprocessError,
            gate.TrainCancelled):
        return None


def split_diagnosis(members, ids, judge):
    """Visit ordered halves to depth two; only positive singletons earn blame."""
    result = dict(culprits=[], innocent=[], requeued=[], depth=0)
    proven = {}

    def visit(group, depth):
        result['depth'] = max(result['depth'], depth)
        try:
            states = judge(group, depth)
            valid = (isinstance(states, dict) and set(states) == set(ids)
                     and all(isinstance(s, str) and s in attribution.STATES for s in states.values()))
        except (ValueError, OSError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError,
                gate.TrainCancelled):
            valid, states = False, {}
        print(json.dumps(dict(visit=group, depth=depth, determined=valid)), file=sys.stderr)
        if not valid or all(s == 'undetermined' for s in states.values()):
            result['requeued'].extend(group)
        elif all(s in ('green', 'absent') for s in states.values()):
            result['innocent'].extend(group)
        elif len(group) == 1:
            red = [n for n in ids if valid and states[n] == 'red']
            if red:
                proven[group[0]] = red
                result['culprits'].extend(group)
            else:
                result['requeued'].extend(group)
        elif depth == 2:
            result['requeued'].extend(group)
        else:
            middle = len(group) // 2
            visit(group[:middle], depth + 1)
            visit(group[middle:], depth + 1)

    visit(members, 0)
    for key in ('culprits', 'innocent', 'requeued'):
        result[key] = [s for s in members if s in result[key]]
    return result, proven


def diagnosis(repo, row, entries, events, ids, main):
    """Reuse exact covered trees; cache each diagnostic tuple within an invocation."""
    memo = {}
    project, base = row['project'], row['base_sha']
    scoped = [e for e in events if e.get('project') == project]
    root_required = None

    def judge(group, depth):
        nonlocal root_required
        chosen = [e for e in entries if e[0] in group]
        candidate = window_rehearse.prepare(repo, row['subject'], base, chosen, project, main, events)
        tree = gate.tree_of(repo, candidate)
        required = gate.scope(repo, base, candidate, gate.required_checks(repo, base))['checks']
        if depth == 0:
            root_required = required
        key = (tree, tuple(sorted(required)), tuple(ids))
        if key in memo:
            return memo[key]
        prior = window_members.prior_result(scoped, tree)
        if gate.covers(prior, required):
            answer = dict.fromkeys(ids, 'green')
            print(json.dumps(dict(reused=group, tree=tree, kind='covered-green')), file=sys.stderr)
        elif (prior and prior.get('green') is False and isinstance(prior.get('required'), list)
              and all(isinstance(c, str) for c in prior['required'])
              and set(required) <= set(prior['required'])
              and isinstance(prior.get('nodes', prior.get('new_ids', [])), list)
              and all(isinstance(n, str) for n in prior.get('nodes', prior.get('new_ids', [])))
              and set(ids) <= set(prior.get('nodes', prior.get('new_ids', [])))):
            answer = dict.fromkeys(ids, 'red')
            print(json.dumps(dict(reused=group, tree=tree, kind='covered-red')), file=sys.stderr)
        else:
            response = attribution.probe(repo, candidate, ids)
            require(isinstance(response, dict) and response.get('sha', candidate) == candidate,
                    'diagnostic identity mismatch')
            answer = response.get('states')
        memo[key] = answer
        return answer

    result, proven = split_diagnosis(row['specs'], ids, judge)
    # The admitted rehearsal may already hold a positive interaction proof from
    # the existing policy. Its conservative (unproven) drop list is never blame.
    existing = row.get('attribution', {})
    # Shape only for the shared freshness/coverage checks, never merge evidence.
    proof_identity = window_members.prior_result(
        [dict(row, type='window-tree-verified', green=True)], row['tree'])
    if (root_required is not None and gate.covers(proof_identity, root_required)
            and isinstance(existing, dict) and existing.get('mode') in ('singles', 'pairs')):
        owners = existing.get('proven', {})
        require(isinstance(owners, dict) and set(owners) <= set(row['specs']),
                'invalid existing attribution')
        for spec, nodes in owners.items():
            charged = [n for n in ids_list(nodes) if n in ids]
            if charged:
                proven[spec] = [n for n in ids if n in proven.get(spec, []) or n in charged]
    result['culprits'] = [s for s in row['specs'] if s in proven]
    for key in ('innocent', 'requeued'):
        result[key] = [s for s in result[key] if s not in proven]
    return result, proven


def context(args):
    require(PROJECT.fullmatch(args.project) and REHEARSAL.fullmatch(args.rehearsal), 'invalid identity')
    supplied = members_list(args.specs.split(','))
    ids = ids_list(read_json(args.failed_ids, limit=LIMIT))
    configured = os.environ.get('DOIT_BISECT_QUEUE_DIR')
    ready_config = os.environ.get('DOIT_BISECT_READY_FILE')
    require(args.project == 'albert-scott' or configured and ready_config,
            'project needs isolated lane configuration')
    queue = directory(configured or '/var/tmp/push/rq')
    require(queue.is_dir(), 'lane directory missing')
    ready = regular(ready_config or '/var/tmp/push/ready-pass.txt', absent=True)
    require(ready.parent.is_dir(), 'ready parent missing')
    path = Path(args.batch)
    batch = regular(path if path.is_absolute() else queue / path, absent=True)
    require(batch.parent == queue and re.fullmatch(r'[0-9]+\.batch\.red', batch.name), 'invalid batch path')
    for name in ('attention.txt', '.bisect.lock', 'bisect-protocol.json'):
        regular(queue / name, absent=True)
    for name in ('bisect-state', 'bisect-retry'):
        directory(queue / name)
    require(ready.parent != queue and ready not in (batch, queue / 'attention.txt', queue / '.bisect.lock',
                          queue / 'bisect-protocol.json') and ready.parent != queue / 'bisect-state'
            and ready.parent != queue / 'bisect-retry', 'ready path overlaps queue artifacts')
    events = admitted_events()
    records = [e for e in events if e.get('type') == 'window-rehearsed'
               and e.get('subject') == args.rehearsal and e.get('project') == args.project
               and e.get('state') not in ('prepared', 'running')]
    require(records, 'admitted rehearsal not found')
    row = records[-1]
    keys = ('project', 'specs', 'state', 'base_sha', 'sha', 'tree', 'required', 'ids', 'reason', 'attribution')
    require(all({k: e.get(k) for k in keys}
                == {k: row.get(k) for k in keys}
                for e in records), 'conflicting rehearsal records')
    require(supplied == members_list(row.get('specs')), 'recorded member order mismatch')
    require(set(ids) == set(ids_list(row.get('ids', []))), 'authoritative failing-id mismatch')
    state = row.get('state')
    require(state in ('red', 'undetermined', 'refused'), 'rehearsal state cannot be disposed')
    require(state != 'refused' or row.get('reason') == 'rehearsal-cap', 'unsupported refusal')
    existing = row.get('attribution', {})
    require(isinstance(existing, dict), 'invalid recorded attribution')
    owners = existing.get('proven', {})
    require(isinstance(owners, dict) and set(owners) <= set(supplied)
            and all(ids_list(nodes) and set(nodes) <= set(ids) for nodes in owners.values()),
            'invalid recorded attribution proof')
    if state == 'red':
        require(all(isinstance(row.get(k), str) and SHA.fullmatch(row[k])
                    for k in ('base_sha', 'sha', 'tree')) and isinstance(row.get('required'), list)
                and all(isinstance(c, str) and c for c in row['required']), 'red candidate identity missing')
    protocol(queue)
    return queue, ready, batch, row, ids, events


def load_source(queue, batch, row):
    """Join sidecar to a committed request, or recover a journaled source."""
    project, ident, members = row['project'], row['subject'], row['specs']
    sidecar = regular(queue / (batch.name[:-4] + '.lineage.json'), absent=True)
    ci = regular(Path(str(batch) + '.ci'), absent=True)
    if not batch.exists():
        for path in sorted((queue / 'bisect-state').glob('*.json')):
            state = read_json(path)
            if ident in state.get('sources', {}):
                source = state['sources'][ident]
                require(state.get('project') == project and source.get('batch') == batch.name
                        and source.get('members') == members, 'recovery source mismatch')
                validate_journal(state, source, row)
                return path, state, source
        raise ValueError('batch missing without recovery evidence')
    require(ci.is_file(), 'red CI companion missing')
    require(batch.stat().st_size < LIMIT and ci.stat().st_size < LIMIT, 'oversized batch artifacts')
    fields = batch.read_text().split()
    require(len(fields) == len(members) + 1 and SHA.fullmatch(fields[0])
            and fields[1:] == [s[7:] for s in members], 'batch member row mismatch')
    require(row['state'] != 'red' or fields[0] == row['base_sha'], 'recorded base mismatch')
    meta = read_json(sidecar) if sidecar.exists() else None
    root, root_members, attempt = ident, members, 0
    if meta is not None:
        require(isinstance(meta, dict)
                and set(meta) == {'version', 'request_id', 'project', 'lineage', 'root_rehearsal',
                                  'rehearsal', 'members', 'attempt'}
                and type(meta.get('version')) is int and meta['version'] == 1 and meta.get('project') == project
                and meta.get('rehearsal') == ident and meta.get('members') == members
                and isinstance(meta.get('root_rehearsal'), str) and REHEARSAL.fullmatch(meta['root_rehearsal'])
                and isinstance(meta.get('lineage'), str) and HEX.fullmatch(meta['lineage'])
                and type(meta.get('attempt')) is int and 0 <= meta['attempt'] <= 2,
                'invalid lineage sidecar')
        root, attempt = meta['root_rehearsal'], meta['attempt']
        if meta.get('request_id') is not None:
            request_id = meta['request_id']
            require(isinstance(request_id, str) and re.fullmatch(r'rh-[A-Za-z0-9_-]+\.(whole|singleton|deferred)\.[0-9]+', request_id),
                    'invalid retry request identity')
            req = read_json(queue / 'bisect-retry' / (request_id + '.json'))
            require(type(req.get('version')) is int and req['version'] == 1
                    and type(req.get('attempt')) is int
                    and req.get('request_id') == request_id and req.get('project') == project
                    and req.get('lineage') == meta['lineage'] and req.get('root_rehearsal') == root
                    and req.get('members') == members and req.get('attempt') == attempt
                    and req.get('state') in ('claimed', 'acked')
                    and req.get('claimed_rehearsal') == ident
                    and req.get('claimed_batch') == batch.name[:-4], 'retry claim identity mismatch')
            parent = read_json(queue / 'bisect-state' / (meta['lineage'] + '.json'))
            root_members = members_list(parent.get('members'))
            require(parent.get('project') == project and parent.get('root_rehearsal') == root
                    and all(s in root_members for s in members), 'retry lineage mismatch')
            original = parent.get('sources', {}).get(req.get('source_rehearsal'), {}).get('plan', {}).get('requests', [])
            immutable = lambda value: {k: v for k, v in value.items()
                                      if k not in ('state', 'claimed_batch', 'claimed_rehearsal')}
            require(any(immutable(r) == immutable(req) for r in original), 'uncommitted retry request')
        else:
            require(root == ident and attempt == 0, 'ordinary lineage mismatch')
    key = lineage(project, root, root_members)
    require(meta is None or meta['lineage'] == key, 'lineage digest mismatch')
    path = regular(queue / 'bisect-state' / (key + '.json'), absent=True)
    state = read_json(path) if path.exists() else dict(version=1, lineage=key, project=project,
        root_rehearsal=root, members=root_members, attempt=attempt, sources={})
    require(state.get('project') == project and state.get('lineage') == key
            and state.get('root_rehearsal') == root and state.get('members') == root_members,
            'state identity mismatch')
    source = state['sources'].get(ident)
    if source is None:
        source = dict(batch=batch.name, members=members, attempt=attempt,
                      preimages={batch.name: batch.read_text(), ci.name: ci.read_text()}, complete=False)
        if meta is not None:
            source['preimages'][sidecar.name] = sidecar.read_text()
    require(source.get('batch') == batch.name and source.get('members') == members, 'source identity mismatch')
    validate_journal(state, source, row)
    return path, state, source


def validate_journal(state, source, row):
    """Persisted recovery inputs receive the same fencing as new CLI inputs."""
    root_members = members_list(state['members'])
    root = state['root_rehearsal']
    require(isinstance(root, str) and REHEARSAL.fullmatch(root)
            and type(state.get('version')) is int and state['version'] == 1 and state.get('project') == row['project']
            and state.get('lineage') == lineage(row['project'], root, root_members)
            and type(state.get('attempt')) is int and 0 <= state['attempt'] <= 2
            and isinstance(state.get('sources'), dict), 'invalid recovery lineage')
    require([s for s in root_members if s in row['specs']] == row['specs']
            and type(source.get('attempt')) is int and 0 <= source['attempt'] <= 2
            and type(source.get('complete')) is bool, 'invalid recovery source')
    name = source['batch']
    require(isinstance(name, str) and re.fullmatch(r'[0-9]+\.batch\.red', name), 'invalid recovery batch')
    allowed = {name, name + '.ci', name[:-4] + '.lineage.json'}
    images = source.get('preimages')
    require(isinstance(images, dict) and {name, name + '.ci'} <= set(images) <= allowed
            and all(isinstance(v, str) and len(v.encode()) <= LIMIT for v in images.values()),
            'invalid recovery preimages')
    manifest = source.get('plan')
    require(not source['complete'] or manifest is not None, 'completion without plan')
    if manifest is None:
        return
    result = manifest['result']
    require(set(result) == {'culprits', 'innocent', 'requeued', 'depth'}
            and type(result['depth']) is int and 0 <= result['depth'] <= 2,
            'invalid disposition result')
    flattened = []
    for key in ('culprits', 'innocent', 'requeued'):
        group = result[key]
        require(isinstance(group, list) and group == [s for s in row['specs'] if s in group],
                'invalid disposition membership')
        flattened.extend(group)
    require(len(flattened) == len(row['specs']) and set(flattened) == set(row['specs']),
            'incomplete disposition')
    require(isinstance(manifest['proven'], dict) and set(manifest['proven']) == set(result['culprits'])
            and all(ids_list(nodes) and set(nodes) <= set(row.get('ids', []))
                    for nodes in manifest['proven'].values()), 'invalid persisted proof')
    require(set(manifest['reasons']) == set(row['specs'])
            and all(v in ('known-base-failures', 'proven-innocence', 'refused-cap-deferral',
                          'first-whole-uncertainty', 'subsequent-singleton-uncertainty', 'depth-cap-residual')
                    for v in manifest['reasons'].values()), 'invalid release reasons')
    require(type(manifest['attempt']) is int and 0 <= manifest['attempt'] <= 2
            and isinstance(manifest['requests'], list), 'invalid retry plan')
    requested = []
    for req in manifest['requests']:
        group = members_list(req['members'])
        kind = req['kind']
        require(kind in ('whole', 'singleton', 'deferred')
                and (kind != 'singleton' or len(group) == 1), 'invalid retry kind')
        index = root_members.index(group[0]) if kind == 'singleton' else 0
        require(req == dict(version=1, request_id=row['subject']+'.'+kind+'.'+str(index),
                project=row['project'], lineage=state['lineage'], root_rehearsal=root,
                source_rehearsal=row['subject'], source_batch=name, members=group, kind=kind,
                attempt=manifest['attempt'], state='pending', claimed_batch=None, claimed_rehearsal=None),
                'invalid retry artifact')
        requested.extend(group)
    require(requested == result['requeued'], 'incomplete retry plan')
    owned = source.get('attention_rows', {})
    require(isinstance(owned, dict) and set(owned) <= set(result['culprits']), 'invalid attention ownership')
    for spec, text in owned.items():
        require(isinstance(text, str) and re.fullmatch(re.escape(spec[7:])+r' [0-9]{2}:[0-9]{2} attributed '
                + re.escape(json.dumps(manifest['proven'][spec])) + r' \|bisect:v1 batch='
                + re.escape(row['subject']) + ' lineage=' + re.escape(state['lineage']) + r'\|\n', text),
                'forged attention ownership')


def plan(row, result, proven, reason, state, source):
    members, attempt = row['specs'], source['attempt']
    retry = result['requeued']
    kind = 'singleton'
    if reason == 'refused-cap':
        kind = 'deferred'
    elif reason == 'uncertainty':
        kind = 'whole' if attempt == 0 and len(members) > 1 else 'singleton'
        attempt = 1 if kind == 'whole' else 2
    elif retry:
        attempt = 2
    groups = [retry] if kind in ('whole', 'deferred') and retry else [[s] for s in retry]
    requests = []
    for group in groups:
        index = state['members'].index(group[0]) if kind == 'singleton' else 0
        ident = row['subject'] + '.' + kind + '.' + str(index)
        requests.append(dict(version=1, request_id=ident, project=row['project'], lineage=state['lineage'],
            root_rehearsal=state['root_rehearsal'], source_rehearsal=row['subject'], source_batch=source['batch'],
            members=group, kind=kind, attempt=attempt, state='pending', claimed_batch=None, claimed_rehearsal=None))
    why = ('refused-cap-deferral' if reason == 'refused-cap' else
           'first-whole-uncertainty' if kind == 'whole' else
           'subsequent-singleton-uncertainty' if reason == 'uncertainty' else 'depth-cap-residual')
    return dict(result=result, proven=proven, requests=requests, attempt=attempt,
                reasons={s: ('known-base-failures' if reason == 'known-base' else 'proven-innocence')
                         if s in result['innocent'] else why for s in members})


def persist(queue, ready, batch, row, path, state, source):
    """Journal first, then idempotent artifacts/events, then retire exact preimages."""
    manifest = source['plan']
    validate_journal(state, source, row)
    state['sources'][row['subject']] = source
    state['attempt'] = max(state['attempt'], manifest['attempt'])
    directory(path.parent).mkdir(exist_ok=True)
    directory(queue / 'bisect-retry').mkdir(exist_ok=True)
    save(path, state)
    for req in manifest['requests']:
        target = regular(queue / 'bisect-retry' / (req['request_id'] + '.json'), absent=True)
        if target.exists():
            prior = read_json(target)
            require(all(prior.get(k) == v for k, v in req.items()
                        if k not in ('state', 'claimed_batch', 'claimed_rehearsal'))
                    and prior.get('state') in ('pending', 'claimed', 'acked'), 'conflicting retry request')
        else:
            save(target, req)
    content = ready.read_bytes() if ready.exists() else b''
    present = set(content.decode().splitlines())
    additions = [s[7:] for s in manifest['result']['innocent'] if s[7:] not in present]
    if additions:
        atomic(ready, content + (b'\n' if content and not content.endswith(b'\n') else b'')
               + ('\n'.join(additions) + '\n').encode())
    attention = regular(queue / 'attention.txt', absent=True)
    original = attention.read_bytes() if attention.exists() else b''
    lines = original.splitlines(keepends=True)
    marker = '|bisect:v1 batch=' + row['subject'] + ' lineage=' + state['lineage'] + '|'
    for spec, ids in manifest['proven'].items():
        # Only byte-for-byte rows created by this transaction are ours to replace.
        owned = source.setdefault('attention_rows', {})
        if spec not in owned:
            owned[spec] = (spec[7:] + ' ' + datetime.now(timezone.utc).strftime('%H:%M')
                           + ' attributed ' + json.dumps(ids) + ' ' + marker + '\n')
    save(path, state)
    for text in source.get('attention_rows', {}).values():
        encoded = text.encode()
        if encoded not in lines:
            if lines and not lines[-1].endswith(b'\n'):
                lines.append(b'\n')
            lines.append(encoded)
    if b''.join(lines) != original:
        atomic(attention, b''.join(lines))
    events = admitted_events()
    for spec in row['specs']:
        park = spec in manifest['proven']
        kind = 'batch-member-parked' if park else 'batch-member-released'
        matching = [e for e in events if e.get('type') == kind and e.get('spec') == spec
                    and e.get('project') == row['project'] and e.get('batch') == row['subject']
                    and e.get('lineage') == state['lineage']]
        fields = dict(spec=spec, batch=row['subject'], project=row['project'], lineage=state['lineage'])
        fields.update(dict(attributed=True, ids=manifest['proven'][spec]) if park
                      else dict(why=manifest['reasons'][spec]))
        require(not matching or all(all(e.get(k) == v for k, v in fields.items()) for e in matching),
                'conflicting disposition event')
        if not matching:
            gate.ev(kind, spec, **fields)
            require(any(e.get('type') == kind and all(e.get(k) == v for k, v in fields.items())
                        for e in admitted_events()), 'ledger persistence incomplete')
    # Record the equivalent of COUNT(*) before removing exactly these owned artifacts.
    source['retirement_count'] = sum((queue / name).exists() for name in source['preimages'])
    save(path, state)
    for name, preimage in source['preimages'].items():
        target = regular(queue / name, absent=True)
        require(target.parent == queue and name in (batch.name, batch.name + '.ci',
                batch.name[:-4] + '.lineage.json'), 'foreign recovery artifact')
        if target.exists():
            require(target.read_text() == preimage, 'source changed during disposition')
            target.unlink()
    parent = os.open(queue, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    source['complete'] = True
    save(path, state)
    return manifest['result']


def run(args):
    queue, ready, batch, row, ids, events = context(args)
    # Fence every new red identity before creating even the stable lock file.
    # A durable plan already passed this check; recovery must survive moved refs.
    _, _, inspected = load_source(queue, batch, row)
    entries = None
    repo = directory(fold.ROOT / 'repos' / row['project'])
    if row['state'] == 'red' and 'plan' not in inspected:
        entries = gate.resolve_entries(repo, row['specs'], events)
        candidate = window_rehearse.prepare(repo, row['subject'], row['base_sha'], entries,
                                             row['project'], args.main, events)
        require(gate.tree_of(repo, candidate) == row['tree'], 'rebuilt rehearsal tree mismatch')
        require(gate.tree_of(repo, row['sha']) == row['tree'], 'recorded candidate tree mismatch')
    with lane_lock(queue):
        protocol(queue)
        path, state, source = load_source(queue, batch, row)
        if source.get('complete'):
            return source['plan']['result']
        if 'plan' in source:
            return persist(queue, ready, batch, row, path, state, source)
    result = dict(culprits=[], innocent=[], requeued=row['specs'], depth=0)
    proven, reason = {}, 'uncertainty'
    if row['state'] == 'refused':
        reason = 'refused-cap'
    elif row['state'] == 'red' and ids:
        new = new_failures(row['project'], row['base_sha'], ids)
        if new == []:
            result.update(innocent=row['specs'], requeued=[])
            reason = 'known-base'
        elif new is not None:
            result, proven = diagnosis(repo, row, entries, events, new, args.main)
            reason = 'uncertainty' if result['depth'] == 0 and result['requeued'] == row['specs'] else 'diagnosis'
    with lane_lock(queue):
        protocol(queue)
        path, state, current = load_source(queue, batch, row)
        if current.get('complete'):
            return current['plan']['result']
        if 'plan' not in current:
            current['plan'] = plan(row, result, proven, reason, state, current)
        return persist(queue, ready, batch, row, path, state, current)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('rehearsal')
    parser.add_argument('--project', required=True)
    parser.add_argument('--specs', required=True)
    parser.add_argument('--failed-ids', required=True)
    parser.add_argument('--batch', required=True)
    parser.add_argument('--main', default='master')
    args = parser.parse_args(argv)
    try:
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', args.main)
                and '..' not in args.main, 'invalid main branch')
        with diagnostics():
            result = run(args)
        print(json.dumps(result))
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, AttributeError, IndexError, UnicodeError,
            subprocess.SubprocessError, gate.TrainCancelled) as exc:
        # File/subprocess exceptions may contain arbitrary credential-bearing bytes.
        print('window-bisect: refused or incomplete (' + type(exc).__name__ + ')', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
