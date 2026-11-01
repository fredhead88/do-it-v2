"""Strict structured Thinker/operator decisions; prose never supplies a grant."""
import json
import re
import shlex

KINDS = {'base-red-accepted', 'drop-from-window', 'regrade', 'rebuild', 'grant-extend'}
SPEC = re.compile(r'L-spec-\d{4}')
NODE = re.compile(r'[A-Za-z0-9_./-]+\.py::[^\s\x00-\x1f]+')


def validate(value):
    if (not isinstance(value, dict) or not isinstance(value.get('kind'), str)
            or value['kind'] not in KINDS):
        raise ValueError('unknown ruling kind')
    kind = value['kind']
    allowed = {'kind', 'reason'} | ({'nodes'} if kind == 'base-red-accepted' else {'spec'})
    allowed |= {'paths'} if kind == 'grant-extend' else {'window'} if kind == 'drop-from-window' else set()
    if set(value) - allowed:
        raise ValueError('unknown ruling fields')
    if 'reason' in value and (not isinstance(value['reason'], str) or not value['reason'].strip()):
        raise ValueError('ruling needs a nonempty reason')
    if kind == 'base-red-accepted':
        nodes = value.get('nodes')
        if not isinstance(nodes, list) or not nodes or any(not isinstance(n, str) or not NODE.fullmatch(n) for n in nodes):
            raise ValueError('base-red-accepted needs pytest node ids')
        return dict(value, nodes=sorted(set(nodes)))
    if not isinstance(value.get('spec'), str) or not SPEC.fullmatch(value['spec']):
        raise ValueError('ruling needs an L-spec-NNNN target')
    if kind == 'drop-from-window':
        if not value.get('reason'):
            raise ValueError('drop-from-window needs a reason')
        if 'window' in value and (not isinstance(value['window'], str)
                                 or not re.fullmatch(r'[A-Za-z0-9._-]+', value['window'])
                                 or value['window'] in ('.', '..')):
            raise ValueError('invalid ruling window')
    if kind == 'grant-extend':
        paths = value.get('paths')
        if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not p for p in paths):
            raise ValueError('grant-extend needs paths')
    return dict(value)


def parse(answer, *, subject=None, window=None):
    """Only JSON or a leading canonical kind is executable. Other prose stays prose."""
    if answer.lstrip().startswith('{'):
        try:
            value = json.loads(answer)
        except json.JSONDecodeError:
            if re.search(r'"kind"\s*:', answer):
                raise
            return None
        return validate(value) if 'kind' in value else None
    first = answer.strip().split(maxsplit=1)
    if not first or first[0].rstrip(':').lower() not in KINDS:
        return None
    words = shlex.split(answer)
    kind, words = words[0].rstrip(':').lower(), words[1:]
    result = {'kind': kind}
    if '--reason' in words:
        index = words.index('--reason')
        result['reason'] = ' '.join(words[index+1:])
        words = words[:index]
    if '--window' in words:
        index = words.index('--window')
        if index+1 >= len(words):
            raise ValueError('--window needs a window id')
        result['window'] = words[index+1]
        words = words[:index]+words[index+2:]
    if kind == 'base-red-accepted':
        result['nodes'] = [n for word in words for n in word.split(',') if n]
    else:
        result['spec'] = words.pop(0) if words and SPEC.fullmatch(words[0]) else subject
        if kind == 'grant-extend':
            result['paths'] = words
        elif words:
            result['reason'] = ' '.join(words)
        if kind == 'drop-from-window' and window and 'window' not in result:
            result['window'] = window
    return validate(result)


def ruling(event):
    if event.get('type') != 'decision' or event.get('actor') not in ('thinker', 'operator'):
        return None
    try:
        return validate(event.get('ruling'))
    except (ValueError, TypeError):
        return None


def decisions(events, project):
    return [(e, r) for e in events if e.get('project') == project for r in [ruling(e)] if r]


def project_event(event):
    value = ruling(event)
    if value is None or value['kind'] not in ('rebuild', 'regrade'):
        return event
    field = 'rework' if value['kind'] == 'rebuild' else 'regrade'
    # Project onto the exact existing Executor/tick triggers, retaining source/ref.
    return dict(event, subject=value['spec'], **{field: 'yes'})


def drops(events, project, window):
    consumed = {e.get('ref') for e in events if e.get('type') == 'window-dropped'
                and e.get('reason') == 'thinker-ruling'}
    return [(e, r) for e, r in decisions(events, project)
            if r['kind'] == 'drop-from-window' and e.get('_src') not in consumed
            and (not r.get('window') or r['window'] == window)]
