"""One dependency-id validator and fail-closed parsing for spec metadata."""
import re


def valid_dependency(value):
    """Dependency holds name exactly one numeric spec identifier."""
    return isinstance(value, str) and re.fullmatch(r'L-spec-[0-9]+', value) is not None


def from_spec_text(text):
    """Return the sole declared prerequisite, ignoring fenced examples.

    A sentence after the value is prose (as in spec 1907); comma/list values
    and repeated declarations are invalid, never silently reduced to one id.
    """
    declarations, fence = [], None
    for line in text.splitlines():
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            continue
        if fence:
            continue
        match = re.match(r'^\s*(?:\*\*)?Dependency-hold(?:\*\*)?:\s*(?:\*\*)?(.*)$', line, re.I)
        if match:
            declarations.append(match[1])
    if not declarations:
        return None
    if len(declarations) != 1:
        raise ValueError('Dependency-hold must declare exactly one prerequisite')
    match = re.fullmatch(r'''waits_for\s*=\s*("[^"]*"|'[^']*'|[^\s]+?)(?:\.\s+.*)?\s*''', declarations[0])
    value = match[1] if match else None
    if value and value[:1] in ('"', "'"):
        value = value[1:-1]
    if not valid_dependency(value):
        raise ValueError('Dependency-hold waits_for must be one string matching L-spec-[0-9]+')
    return value


def _text_declaration(spec_events):
    """Return a synthetic declaration and any fail-closed text diagnostic."""
    import autodispatch
    import dispatch
    own = list(spec_events)
    latest = max((e for e in own if e.get('type') in ('spec-written', 'spec-amended')),
                 key=autodispatch._hold_order, default=None)
    if latest is None or not valid_dependency(latest.get('subject')):
        return None
    sid = latest['subject']
    declaration = dict(latest, type='dependency-hold',
                       why='Dependency-hold declared in spec text', _src='spec-text:' + sid)
    try:
        dependency = from_spec_text(dispatch.spec_path(sid).read_text())
    except FileNotFoundError:
        return None  # Ordinary packet validation owns a missing spec.
    except (OSError, UnicodeError):
        declaration['_dependency_error'] = 'held: spec dependency metadata unreadable'
    except ValueError:
        declaration['_dependency_error'] = ('held: invalid Dependency-hold waits_for in spec text; '
                                             'declare one spec id')
    else:
        if dependency is None:
            return None
        declaration['waits_for'] = dependency
    return declaration


def text_hold_reason(spec_events, all_events, *, shipped=None):
    """Compatibility projection of text alone, with the shared release semantics."""
    import autodispatch
    own = list(spec_events)
    declaration = _text_declaration(own)
    if declaration is None:
        return None
    # Project the already-read declaration; do not re-enter the combined reader.
    rows = autodispatch._unresolved_holds([declaration], own, all_events, shipped=shipped)
    return autodispatch._dependency_reason(rows, all_events)
