"""No-spend readiness gate for first builds, invoked by the tick launch path."""
import argparse
import builtins
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import coupled_tests
import effective_writes
import fold
import grading_env
import merge_gate
import spec_preflight
import verification


class NotReady(ValueError):
    pass


def git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True,
                            text=True, timeout=30, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))
    if result.returncode:
        raise NotReady('repository evidence unavailable: ' + args[0])
    return result.stdout


def _text(root, event):
    path = Path(event.get('path') or root / 'content' / (event['subject'] + '.md'))
    return (path if path.is_absolute() else root / path).read_text()


def inputs(text):
    """Existing inputs, never Produces/Writes or future acceptance assertions."""
    active = False
    section = None
    lines = []
    for line in text.splitlines():
        heading = re.match(r'^\s*(?:#+\s*(?:\d+[.)]?\s*)?)?(Reads|Consumes|Produces|Writes|Interfaces|Boundaries)\b', line, re.I)
        if heading:
            active = heading[1].lower() in ('reads', 'consumes')
            section = heading[1].lower()
        elif re.match(r'^\s*#+\s|^\s*[A-Z][\w ]*:\s*$', line):
            active = False
            section = None
        # The "existing ..." phrases add a line only under an input-bearing heading.
        phrase = section in ('reads', 'consumes', 'interfaces') and re.search(
            r'\balready exists?\b|\bexisting (?:function|symbol|file)\b', line, re.I)
        if active or phrase:
            lines.append(line)
    return lines


JS_BUILTINS = frozenset((
    'Number', 'String', 'Boolean', 'Array', 'Object', 'Math', 'JSON', 'Date', 'Promise', 'Map', 'Set',
    'parseInt', 'parseFloat', 'isNaN', 'now', 'len'))


def _created(text, grant):
    """Names the spec itself creates: Produces identifiers and every granted path."""
    production = re.search(r'(?ims)^\s*Produces:\s*(.*?)(?=^\s*(?:Writes|Consumes|Reads):|^#+\s|\Z)', text)
    names = set(grant)
    for quoted in re.findall(r'`([^`\n]*)`', production[1] if production else ''):
        head = re.match(r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*', quoted.strip())
        if head:
            names.update((head[0], head[0].rsplit('.', 1)[-1]))
    return names


def _defined_anywhere(repo, base, name):
    """True when a def/class/function/const/let/var/type/interface/enum of `name` exists in the base tree."""
    pattern = ('(^|[^[:alnum:]_$])(def|class|function|const|let|var|type|interface|enum)[[:space:]]+[*]?'
               + name.replace('$', '[$]') + '([^[:alnum:]_$]|$)')
    found = subprocess.run(['git', '-C', str(repo), 'grep', '-q', '-E', '-e', pattern, base, '--',
                            '*.py', '*.ts', '*.tsx', '*.js', '*.jsx', ':(exclude,glob)**/node_modules/**'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                           env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))
    if found.returncode not in (0, 1):
        raise NotReady('symbol lookup undetermined')
    return found.returncode == 0


def _sources(text):
    """Source keys qualify only explicit key/reference citations."""
    sources = {}
    for line in text.splitlines():
        match = re.match(r'\s*(?:-\s*)?(?:Source\s+)?([A-Z][A-Za-z0-9]*):\s*(.*)', line)
        if match:
            sources[match[1]] = re.findall(
                r'(?<![\w/])(?:/?[\w.-]+/)+[\w.-]+(?:[:]{1,2}[\w.,]+)?', match[2])
    return sources


def _expand_citations(line, sources):
    def expand(match):
        key, reference = match[1], match[2]
        if key not in sources:
            return match[0]
        module, dot, symbol = reference.rpartition('.')
        candidates = []
        for target in sources[key]:
            path = target.split(':', 1)[0]
            if Path(path).name == reference:
                candidates.append(path)
            elif dot and Path(path).stem == module and Path(path).suffix in ('.py', '.ts', '.tsx', '.js'):
                candidates.append(path + '::' + symbol)
        candidates = list(dict.fromkeys(candidates))
        if len(candidates) != 1:
            raise NotReady('source ownership unresolved: ' + match[0])
        return candidates[0]
    return re.sub(r'\b([A-Z][A-Za-z0-9]*)/([\w]+(?:\.[\w]+)+)', expand, line)


def missing_inputs(text, repo, base, tree, *, grant=None):
    missing = []
    tree_set = set(tree)
    top_directories = {path.split('/', 1)[0] for path in tree if '/' in path}
    project = Path(repo).name
    registrations = Path(repo).parent
    projects = {p.name for p in registrations.iterdir() if p.is_dir()}
    sources = _sources(text)
    if grant is None:
        try:
            grant = merge_gate.grant_from_text(text)
        except merge_gate.Undetermined as exc:
            if 'states no writes:' not in str(exc):
                raise
            grant = []
    created = _created(text, grant)
    packages = set()
    for path in tree:
        if path.endswith('package.json') and 'node_modules/' not in path:
            data = json.loads(git(repo, 'show', base + ':' + path))
            for field in ('dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'):
                packages.update(data.get(field, {}))
    for original in inputs(text):
        # Outputs declared inline on a Consumes line are not consumed interfaces.
        line = re.split(r'\bProduces:', original, maxsplit=1)[0]
        line = _expand_citations(line, sources)
        # Keep the backticks: deleting a template must not pair its opening
        # delimiter with the next real input's delimiter.
        line = re.sub(r'[^\s`]*[$<>][^\s`]*', '', line)
        tokens = []
        quoted = list(re.finditer(r'`([^`\n]*)`', line))
        for match in quoted:
            value = match[1].rstrip('.,:')
            if not re.search(r'\s', value):
                tokens.append((match.start(1), match.end(1), value))
        bare = re.sub(r'`[^`\n]*`', lambda m: ' ' * len(m[0]), line)
        for match in re.finditer(
                r'(?<![\w:/$])(?:[\w@.-]+/)+[\w.*?@-]+(?:::{0,1}[\w.]+(?:\[[^]\s]+\])?)?', bare):
            value = match[0].rstrip('.,:')
            plain = value.split(':')[0]
            if (not Path(plain).suffix and len(plain.split('/')) == 2
                    and re.sub(r'^\s*(?:[-*]|\d+[.)])?\s*', '', bare).strip().rstrip('.,:') != value):
                continue  # prose such as data/props inside a sentence, not a path
            if Path(plain).suffix or value.removeprefix('./').split('/', 1)[0] in top_directories:
                tokens.append((match.start(), match.start() + len(value), value))
        tokens.sort()
        references = []
        for start, end, value in tokens:
            # Both symbol delimiters refer to the root symbol; [index] and
            # a displayed call signature describe the symbol, not the filename.
            symbol = None
            citation = re.fullmatch(r'(.+?\.[\w]+):{1,2}([A-Za-z_][\w.]*)(?:\[[^]]+\])?(?:\(.*\))?', value)
            if citation:
                value, symbol = citation.groups()
            if (any(c in value for c in '*{}=') or value.startswith(('http', '/'))
                    or re.fullmatch(r'actions/[\w.-]+@[\w.-]+', value)):
                continue
            if not citation and re.match(r'^[\w.-]+:(?!//)[^\s]*/', value):
                value = value.split(':', 1)[1]
            if '/' not in value and not (symbol or value in tree_set):
                continue
            owners = set()
            before, after = line[:start].rstrip('`'), line[end:].lstrip('`')
            prefix = re.search(r'([\w-]+)\s+$', before)
            if prefix and prefix[1] in projects:
                owners.add(prefix[1])
            suffix = re.match(r'\s+in\s+(?:project\s+)?([\w-]+)', after)
            if suffix:
                if suffix[1] not in projects:
                    raise NotReady('project ownership unresolved: ' + suffix[1])
                owners.add(suffix[1])
            references.append([value, symbol, owners])
        # A cited producer and its repeated matching path establish the same
        # ownership, without extending that ownership to unrelated paths.
        if re.search(r'\bL-spec-\d+\b', line):
            for value, _, owners in references:
                for other, _, evidence in references:
                    if other == value:
                        owners.update(evidence)
        resolved, citation_symbols, foreign_modules = [], [], set()
        foreign_interface = False
        for path, symbol, owners in references:
            if len(owners) > 1:
                raise NotReady('conflicting project ownership: ' + path)
            if owners and project not in owners:
                foreign_modules.add(Path(path).stem)
                foreign_interface = True
                continue
            package = '/'.join(path.split('/')[:2]) if path.startswith('@') else path.split('/')[0]
            if package in packages:
                continue
            path = path.removeprefix('./')
            variants = [path, 'dashboard/src/' + path[2:]] if path.startswith('@/') else [path]
            variants += [p + ext for p in list(variants) for ext in ('.py', '.ts', '.tsx', '.js', '/index.ts')]
            found = sorted(tree_set.intersection(variants))
            if not found:
                found = [p for p in tree if any(p.endswith('/' + v) for v in variants)]
            if not found and not any(merge_gate.granted(p, [path]) for p in tree):
                if not merge_gate.granted(path, grant) and path not in created:
                    missing.append(('path', path))
                # An absent self-written file has no existing symbol obligation.
                continue
            resolved += found
            if symbol:
                # An explicit existing path stays strict; a basename may fall back tree-wide.
                citation_symbols.append((symbol, found, bool(tree_set.intersection(variants))))
        symbols = re.findall(r'\b(?:function|def|class|const)\s+([A-Za-z_$][\w$]*)', line)
        symbols += re.findall(r'(?<![\w.-])([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\(', line)
        if not re.search(r'\bevents?\b', line):
            symbols += re.findall(r'`([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)`(?=\s*(?:from|in|\())', line)
        events = set(re.findall(r'\bevents?\s+`([\w-]+)\(', line))
        if re.search(r'\bcensus:', line):
            events.update(re.findall(r'`([\w-]+)\(', line))
        checks = citation_symbols + [(s, None, False) for s in dict.fromkeys(symbols)]
        for symbol, specific, strict in checks:
            name = symbol.rsplit('.', 1)[-1]
            if re.fullmatch(r'[0-9a-f]{7,40}', name) or (specific is None and hasattr(builtins, symbol)):
                continue
            if symbol in created or name in created or (
                    specific is None and symbol.split('.')[0] in JS_BUILTINS):
                continue
            if specific is None and (symbol.split('.')[0] in foreign_modules or (
                    foreign_interface and not resolved and '.' not in symbol
                    and re.search(r'\bL-spec-\d+\b', line))):
                continue
            # Plain event names appear in declared event/census signature lists.
            if specific is None and symbol in events:
                continue
            module_targets = [p for p in tree if Path(p).stem == symbol.split('.')[0]] if '.' in symbol else []
            targets = specific or module_targets or resolved or [p for p in tree if p.endswith(('.py', '.ts', '.tsx', '.js', '.jsx'))]
            found = subprocess.run(['git', '-C', str(repo), 'grep', '-q', '-w', '-F', '-e', name,
                                    base, '--', *targets], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=30,
                                   env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))
            if found.returncode not in (0, 1):
                raise NotReady('symbol lookup undetermined')
            if found.returncode and not strict and _defined_anywhere(repo, base, name):
                continue
            if found.returncode:
                missing.append(('symbol', symbol))
    return list(dict.fromkeys(missing))


def producers(missing, events, root, subject, project):
    shipped = {e.get('subject') for e in events if e.get('type') in ('shipped', 'spec-killed', 'spec-closed')}
    written = {e['subject']: e for e in events if e.get('type') == 'spec-written'
               and e.get('project') == project and e.get('subject') != subject and e.get('subject') not in shipped}
    candidates = []
    for sid, event in written.items():
        try:
            text = _text(root, event)
            grant = merge_gate.grant_from_text(text)
            production = re.search(r'(?ims)^\s*Produces:\s*(.*?)(?=^\s*(?:Writes|Consumes|Reads):|^#+\s|\Z)', text)
            candidates.append((sid, grant, production[1] if production else ''))
        except (OSError, ValueError, merge_gate.Undetermined):
            continue
    result = {}
    for kind, name in missing:
        matches = []
        for sid, grant, production in candidates:
            if kind == 'path' and merge_gate.granted(name, grant) or (
                    kind == 'symbol' and re.search(r'\b' + re.escape(name.rsplit('.', 1)[-1]) + r'\b', production)):
                matches.append(sid)
        result[name] = matches
    return result


def companion_matches(text, grant, repo, base, tree, *, registries=()):
    """Explain blocking companions and reference tests using their sources."""
    tests = sorted(p for p in tree if coupled_tests.is_test(p) and not merge_gate.granted(p, grant))
    sources = coupled_tests.writes_paths(grant, tree)
    matches = [dict(test=test, source=source, kind='named',
                    pattern='local test name for ' + Path(source).stem)
               for test in tests for source in sources if coupled_tests.named(source, test)
               and not coupled_tests.fenced(text, test)]
    references = [source for source in sources if source not in registries]
    matches.extend(coupled_tests.reference_matches(references, tests, repo, base, tree))

    def add(paths, source, rule):
        matches.extend(dict(test=path, source=source, kind='companion', pattern=rule)
                       for path in paths if path in tree)

    for path in grant:
        if path == 'crons.toml':
            add(('src/test_reap_tmp.py', 'src/test_crons.py'), path, 'cron manifest companion')
        if '/migrations/' in path or path.startswith('migrations/'):
            add((p for p in tree if p.endswith('genesis_schema_fingerprint.txt')), path,
                'migration fingerprint companion')
        if path.endswith(('contract.gen.ts', 'contract.schema.json')) or (
                'profit-v2' in path and path.endswith(('types.ts', 'contract.ts'))):
            add((p for p in tree if p.endswith(('contract.gen.ts', 'contract.schema.json'))), path,
                'generated contract companion')
    if re.search(r'\bnew event(?: type)?\b', text, re.I):
        add(('docs/HOW-IT-WORKS.md', 'src/test_howto.py'), '(spec)', 'new event companion')
    return sorted((row for row in matches if not merge_gate.granted(row['test'], grant)),
                  key=lambda row: (row['test'], row['source'], row['kind'], row['pattern']))


def _requirements(matches):
    blocking = {row['test'] for row in matches if row['kind'] != 'reference'}
    run_tests = {row['test'] for row in matches if row['kind'] == 'reference'} - blocking
    return sorted(blocking), sorted(run_tests)


def companion_gaps(text, grant, repo, base, tree, *, registries=()):
    return _requirements(companion_matches(text, grant, repo, base, tree, registries=registries))[0]


SKIPPED_PACKAGE_DIRS = {'node_modules', 'archive', '_archive', '_wip', '.worktrees', '.venv'}


def package_dirs(tree):
    """Live package directories under `tree` (relative) that need node_modules. Archived
    and scratch trees keep a package.json nobody installs (archive/dashboard-bluedot bounced
    L-spec-1997 three times on 2026-11-02), so they are not environment requirements."""
    return [str(p.parent.relative_to(tree)) for p in sorted(tree.rglob('package.json'))
            if not set(p.parts) & SKIPPED_PACKAGE_DIRS]


def verify_base(text, repo, base, root, project, timeout=180):
    chain = verification.block(text)
    if not chain:
        raise NotReady('Verification is missing or empty')
    row = grading_env._project_row(project)
    with tempfile.TemporaryDirectory(prefix='build-readiness-', dir='/var/tmp') as tmp:
        allocation = Path(tmp)
        env = spec_preflight._environment(allocation)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        tree = allocation / 'tree'
        for args in (['clone', '--shared', '--no-checkout', str(repo), str(tree)],
                     ['-C', str(tree), 'checkout', '--detach', base]):
            result = spec_preflight._process(['git', '-c', 'core.hooksPath=' + os.devnull, *args], env=env)
            if result.returncode:
                raise NotReady('Verification base checkout unavailable')
        directories = list(row.get('node_dirs', []))
        if re.search(r'\b(?:npm|npx|vitest|tsc)\b', chain):
            # Archived and scratch trees keep a package.json nobody installs (archive/dashboard-bluedot
            # bounced L-spec-1997 three times on 2026-11-02); only live packages need node_modules.
            directories += package_dirs(tree)
        for directory in dict.fromkeys(directories):
            source = (repo / directory / 'node_modules').resolve()
            target = tree / directory / 'node_modules'
            if not source.is_dir():
                raise NotReady('environment missing: ' + directory + '/node_modules')
            # A writable copy keeps a base check from changing the shared dependency tree.
            import node_modules
            target.parent.mkdir(parents=True, exist_ok=True)
            node_modules.copy_tree(source, target)
        venv = row.get('venv')
        if venv or '.venv/' in chain:
            source = Path(venv) if venv and Path(venv).is_absolute() else repo / (venv or '.venv')
            if not (source / 'bin/python').is_file() and not (source / 'bin/python3').is_file():
                raise NotReady('environment missing: .venv/bin/python')
            (tree / '.venv').symlink_to(source.resolve(), target_is_directory=True)
            env['PATH'] = str(source / 'bin') + os.pathsep + env.get('PATH', os.defpath)
        import build_runner
        chain, _ = build_runner._rewrite(chain, repo, tree, venv)
        result = spec_preflight._process(['/bin/bash', '-eo', 'pipefail', '-c', chain], cwd=tree, env=env, timeout=timeout)
        # New tests and their assertions need not pass (or even exist) on base.
        # pytest internal errors and shell execution failures are environment defects.
        if result.returncode in (3, 126, 127):
            raise NotReady('Verification environment fails on dispatch base (exit ' + str(result.returncode) + ')')
        return ([f'Verification on dispatch base observed exit {result.returncode} (non-blocking)']
                if result.returncode else [])


def check(subject, project, base, events, *, root=None, dry_run=True):
    root = Path(root or fold.ROOT)
    result = dict(spec=subject, base_sha=base, status='ready', findings=[], waits_for=None, run_tests=[])
    own = [e for e in events if e.get('subject') == subject]
    if any(e.get('type') in ('build-started', 'build-done', 'shipped') for e in own):
        result['status'] = 'already-built'
        return result
    try:
        if not re.fullmatch(r'L-spec-\d+', subject) or not re.fullmatch(r'[\w-]+', project):
            raise NotReady('invalid spec or project')
        written = next(e for e in reversed(own) if e.get('type') == 'spec-written')
        text = _text(root, written)
        grant = effective_writes.effective_grant(subject, text, events)
        repo = root / 'repos' / project
        if not re.fullmatch(r'[0-9a-f]{40}', base):
            raise NotReady('dispatch base must be a full commit SHA')
        tree = git(repo, 'ls-tree', '-r', '--name-only', base, '--').splitlines()
        missing = missing_inputs(text, repo, base, tree, grant=grant)
        if missing:
            produced = producers(missing, events, root, subject, project)
            if all(len(ids) == 1 for ids in produced.values()):
                result.update(status='held', waits_for=next(iter(produced.values()))[0])
                result['findings'] = ['missing ' + kind + ': ' + name for kind, name in missing]
                return result
            raise NotReady('missing inputs without a unique producer: ' + ', '.join(name for _, name in missing))
        registries = grading_env._project_row(project).get('shared_append_only_registries', [])
        gaps, result['run_tests'] = _requirements(companion_matches(
            text, grant, repo, base, tree, registries=registries))
        if gaps:
            raise NotReady('Writes omits coupled tests/companions: ' + ', '.join(gaps))
        if dry_run:
            result['notes'] = verify_base(text, repo, base, root, project)
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError,
            coupled_tests.Truncated, merge_gate.Undetermined) as exc:
        result.update(status='bounced', findings=[str(exc) or 'readiness undetermined'])
    return result


def record(result, project, *, root=None):
    import dispatch
    root = Path(root or fold.ROOT)
    dst = root / 'events' / 'L-tick-local.jsonl'
    dst.parent.mkdir(parents=True, exist_ok=True)
    base = dict(subject=result['spec'], project=project)
    kind, fields = 'build-readiness', dict(result)
    fields.pop('spec')
    error = dispatch.emit(dst, base, kind, strict=True, **fields)
    if error:
        raise NotReady(error)
    if result['status'] == 'held':
        if not fold.valid_dependency(result['waits_for']):
            raise NotReady('invalid readiness dependency')
        error = dispatch.emit(dst, base, 'dependency-hold', strict=True,
                              waits_for=result['waits_for'], why='; '.join(result['findings']))
    elif result['status'] == 'bounced':
        error = dispatch.emit(dst, base, 'spec-shape-failed', strict=True, findings=result['findings'])
    if error:
        raise NotReady(error)


def build_done_fields(output):
    """Output-to-ledger fields; dispatch's owner must apply these to build-done."""
    if output.get('status') != 'NEEDS_CONTEXT':
        return {}
    reason = output.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        raise NotReady('NEEDS_CONTEXT requires a nonempty reason')
    return {'reason': reason.strip()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec')
    parser.add_argument('--project', required=True)
    parser.add_argument('--base', required=True)
    args = parser.parse_args(argv)
    events = fold.read_events()
    result = check(args.spec, args.project, args.base, events)
    record(result, args.project)
    print(json.dumps(result))
    return 0 if result['status'] in ('ready', 'already-built') else 2


if __name__ == '__main__':
    raise SystemExit(main())
