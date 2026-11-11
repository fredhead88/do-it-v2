"""Read-only SD10 decisions over declared schema seams and normalized holds."""
from dataclasses import dataclass
import pathlib
import re

import dependency_holds
import rechain


@dataclass
class CutAdvice:
    cut: bool
    consumers: list[str]
    migration_paths: list[str]


def safe_path(value):
    """Validate a relative declaration before any filesystem operation."""
    path = pathlib.PurePosixPath(value)
    if (not value or path.is_absolute() or '..' in path.parts
            or any(p.startswith('.env') for p in path.parts)):
        raise ValueError('unsafe footprint path')
    return path


def matches(repo, value):
    """Resolve only within the supplied repository, including symlink targets."""
    safe_path(value)
    root = pathlib.Path(repo).resolve()
    paths = root.glob(value) if any(c in value for c in '*?[') else [root / value]
    hits = []
    for path in paths:
        if not path.resolve().is_relative_to(root):
            raise ValueError('footprint escapes repository')
        safe_path(path.relative_to(root).as_posix())
        if path.is_file():
            hits.append(path)
    return hits


def is_version(path):
    safe_path(path)
    return (path.startswith(rechain.VERSIONS) and path.endswith('.py')
            and pathlib.PurePosixPath(path).name != '__init__.py')


def schema(signature):
    """Preserve the fully qualified name AND the schema kind; never leaf-match."""
    value = signature.replace('`', '').replace('*', '').strip()
    m = re.fullmatch(r'([^\s`*]+)\s+(column|table|enum)', value)
    if m:
        return m[1], m[2]
    # Function/type signatures are ordinary seams; malformed schema declarations
    # cannot silently become evidence that no schema was declared.
    if re.search(r'\b(column|table|enum)\b', value) and '(' not in value:
        raise ValueError('ambiguous schema declaration')
    return None


def needs_cut(slot, open_specs):
    """Decide SD10 using supplied declarations only; no file or ledger reads."""
    footprint = list(slot.get('footprint', []))
    versions, unresolved = [], []
    for path in footprint:
        safe_path(path)
        if any(c in path for c in '*?['):
            prefix = re.split(r'[*?\[]', path, maxsplit=1)[0]
            if rechain.VERSIONS.startswith(prefix) or prefix.startswith(rechain.VERSIONS):
                unresolved.append(path)
            continue
        if is_version(path):
            versions.append(path)
    versions = sorted(set(versions))
    if unresolved and not versions:
        raise ValueError('unresolved migration grant')
    if not versions:
        return CutAdvice(False, [], [])
    if all(is_version(p) or p in rechain.SHARED or p == rechain.TEST for p in footprint):
        return CutAdvice(False, [], versions)
    creates = {s for s in (schema(v) for v in
               slot.get('migration_produces', slot.get('produces', []))) if s}
    own = {v for v in (slot.get('id'), slot.get('name')) if v}
    consumers = set()
    for other in open_specs:
        identity = other.get('id') or other.get('name')
        if identity in own or not identity:
            continue
        declared = {s for s in (schema(v) for v in other.get('consumes', [])) if s}
        holds = set(other.get('waits_for', []))
        text_hold = dependency_holds.from_spec_text(other.get('text', ''))
        if text_hold:
            holds.add(text_hold)
        if creates & declared or slot.get('id') in holds:
            consumers.add(identity)
    consumers = sorted(consumers)
    size = slot.get('code_exceeds_size')
    if not consumers and size is None:
        raise ValueError('code-size unmeasured for mixed migration unit')
    return CutAdvice(bool(consumers) or size is True, consumers, versions)
