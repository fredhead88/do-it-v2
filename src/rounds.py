"""Pure round vocabulary. Callers own event acquisition and limit enforcement."""
from collections import namedtuple
import re


CONTINUE = 'CONTINUE'
MAX_ROUNDS = 4
SIZE_LINE_FILES = 15
SIZE_LINE_CRITERIA = 8

Measure = namedtuple('Measure', 'writes_files criteria')
Split = namedtuple('Split', 'source round_one')


def _visible_lines(text):
    """Preserve physical positions while blanking fenced Markdown examples."""
    lines, fence = [], None
    for line in text.splitlines():
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if fence is not None:
            if re.fullmatch(r'\s*' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}\s*', line):
                fence = None
            lines.append('')
        elif marker:
            fence = (marker[1][0], len(marker[1]))
            lines.append('')
        else:
            lines.append(line)
    return lines


def _acceptance_heading(line):
    return bool(re.match(r'^#+\s*\d*\.?\s*[^\n]*Acceptance[^\n]*$', line, re.I))


def _criteria(lines):
    """Selected distinct anchor IDs and their lines, following packet.criteria.

    Keep this parser local: importing packet loads the orchestration stack.
    Only an anchor's own line may carry its foundation label.
    """
    anchors, headings, bullets = [], [], False
    for index, line in enumerate(lines):
        if re.match(r'^#+ ', line):
            headings.append(index)
            bullets = _acceptance_heading(line)
        marker = r'(?:(?:[-*+]|\d+[.)])\s+)?' if bullets else ''
        match = re.match(r'\s*' + marker + r'\**(AC\d+)\** \[', line)
        if match:
            anchors.append((match[1], index, line))
    start = next((i for i in headings if _acceptance_heading(lines[i])), None)
    if start is not None:
        end = next((i for i in headings if i > start), len(lines))
        selected = [a for a in anchors if start < a[1] < end]
        if selected:
            anchors = selected
    return anchors


def _label(line, name):
    match = re.match(r'\s*(?:[-*+]\s+)?(?:\*\*|__)?' + name +
                     r'(?:\*\*|__)?\s*:\s*(.*)$', line, re.I)
    if not match:
        return None
    value = match[1]
    if line[:match.start(1)].count('**') % 2 and value.startswith('**'):
        value = value[2:]
    if line[:match.start(1)].count('__') % 2 and value.startswith('__'):
        value = value[2:]
    return value.strip()


def _path_tokens(value):
    return [token for token in re.split(r'[,\s]+', value.replace('`', '').strip()) if token]


def _validate_paths(tokens):
    if not tokens:
        raise ValueError('selected path declaration is empty')
    for token in tokens:
        if (not isinstance(token, str)
                or not re.fullmatch(r'[A-Za-z0-9_.@+*?\[\]()/-]+', token)
                or not any(c in token for c in '/.*?')):
            raise ValueError('selected declaration contains a non-path entry')
    return len(tokens)


def _yaml_path(value):
    """A string member in the supported frontmatter writes-list subset."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _writes_files(text, lines):
    # A present writes key is authoritative even if unusable. This deliberately
    # supports only path lists, without an optional YAML dependency or I/O.
    frontmatter = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
    if frontmatter:
        fm_lines = frontmatter[1].splitlines()
        for i, line in enumerate(fm_lines):
            match = re.match(r'^writes:\s*(.*)$', line)
            if not match:
                continue
            value = match[1].strip()
            if value:
                if not (value.startswith('[') and value.endswith(']')):
                    raise ValueError('frontmatter writes must be a path list')
                members = value[1:-1].split(',') if value[1:-1].strip() else []
                return _validate_paths([_yaml_path(t) for t in members])
            members = []
            for following in fm_lines[i + 1:]:
                if not following.strip():
                    continue
                member = re.match(r'^\s*-\s+(.+?)\s*$', following)
                if member:
                    members.append(_yaml_path(member[1]))
                elif re.match(r'^\S', following):
                    break
                else:
                    raise ValueError('frontmatter writes must be a path list')
            return _validate_paths(members)
    for label in ('Writes', 'Footprint'):
        for i, line in enumerate(lines):
            value = _label(line, label)
            if value is None and re.fullmatch(r'#+\s+' + label + r'\s*', line, re.I):
                value = ''
            if value is None:
                continue
            if value and not re.fullmatch(r'\(.*\)', value):
                return _validate_paths(_path_tokens(value))
            tokens, path_fence = [], None
            # A fence immediately under the declaration can delimit its path
            # block. Fenced examples elsewhere cannot introduce declarations.
            for following in text.splitlines()[i + 1:]:
                marker = re.match(r'^\s*(`{3,}|~{3,})', following)
                if marker:
                    if path_fence is not None or tokens:
                        break
                    path_fence = marker[1]
                    continue
                if re.match(r'^#+ ', following):
                    break
                if not following.strip():
                    if tokens:
                        break
                    continue
                tokens.extend(_path_tokens(re.sub(r'^\s*[-*+]\s+', '', following)))
            return _validate_paths(tokens)
    return 0


def measure(spec_text: str) -> Measure:
    """Count declared entries (never glob matches) and selected distinct IDs."""
    lines = _visible_lines(spec_text)
    ids = {identifier for identifier, _, _ in _criteria(lines)}
    return Measure(_writes_files(spec_text, lines), len(ids))


def planned_rounds(spec_text: str) -> int:
    size = measure(spec_text)
    return 2 if size.writes_files >= SIZE_LINE_FILES or size.criteria > SIZE_LINE_CRITERIA else 1


def round_split(spec_text: str) -> Split:
    lines = _visible_lines(spec_text)
    anchors = _criteria(lines)
    inventory = {identifier for identifier, _, _ in anchors}
    for line in lines:
        value = _label(line, 'Rounds')
        if value is None:
            continue
        match = re.fullmatch(r'1\s*=\s*(AC\d+(?:\s*,\s*AC\d+)*)\s*;\s*2\s*=\s*rest', value)
        if not match:
            raise ValueError('invalid explicit Rounds syntax')
        ids = tuple(dict.fromkeys(re.split(r'\s*,\s*', match[1])))
        if not set(ids) <= inventory:
            raise ValueError('Rounds references an unselected criterion')
        return Split('rounds-line', ids)
    foundation = tuple(dict.fromkeys(identifier for identifier, _, line in anchors
                                     if '[foundation]' in line))
    return Split('foundation-label', foundation) if foundation else Split('builder', ())


def round_of(events: list[dict], subject: str) -> int:
    """Next build ordinal since the last fresh start, regardless of outcome.

    A continuation start preserves the ordinal already advanced by its hand-back.
    Counts above MAX_ROUNDS are returned to the routing consumer for re-cutting.
    Caller order and authorization are authoritative; round metadata is ignored.
    """
    ordinal = 1
    for event in events:
        if event.get('subject') != subject:
            continue
        if event.get('type') == 'build-started' and not event.get('continuation', False):
            ordinal = 1
        elif event.get('type') == 'build-done':
            ordinal += 1
    return ordinal
