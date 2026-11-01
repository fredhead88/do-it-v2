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


def text_hold_reason(spec_events, all_events, *, shipped=None):
    """Project a text declaration at automatic build admission without forging events."""
    import autodispatch
    import dispatch
    own = list(spec_events)
    written = [e for e in own if e.get('type') in ('spec-written', 'spec-amended')]
    if not written:
        return None
    latest = max(written, key=autodispatch._hold_order)
    sid = latest.get('subject')
    if not valid_dependency(sid):
        return None
    # A subsequently recorded hold is authoritative, including its release.
    if any(e.get('type') == 'dependency-hold' and
           autodispatch.fold.ts(e.get('ts')) >= autodispatch.fold.ts(latest.get('ts')) for e in own):
        return None
    try:
        dependency = from_spec_text(dispatch.spec_path(sid).read_text())
    except FileNotFoundError:
        return None  # The ordinary packet/worktree path owns a missing spec.
    except OSError:
        return 'held: spec dependency metadata unreadable'
    except ValueError:
        return 'held: invalid Dependency-hold waits_for in spec text; declare one spec id'
    if dependency is None:
        return None
    declaration = dict(latest, type='dependency-hold', waits_for=dependency,
                       why='Dependency-hold declared in spec text', _src='spec-text:' + sid)
    if autodispatch._hold_released(declaration, own):
        return None
    return autodispatch.held_by_dependency([declaration], all_events, shipped=shipped)
