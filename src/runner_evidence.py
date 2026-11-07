"""Collect independent shell-chain steps without losing their setup context."""
import json
import hashlib
import pathlib
import re
import shlex

import base_red


def split_chain(command):
    """Split top-level && only; quoted text, substitutions and groups stay intact."""
    result, start, index, quote, depth = [], 0, 0, None, 0
    while index < len(command):
        char = command[index]
        if char == '\\' and quote != "'":
            index += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == '#' and (index == 0 or command[index - 1].isspace()):
            end = command.find('\n', index)
            index = len(command) if end < 0 else end
            continue
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif depth == 0 and command[index:index + 2] == '&&':
            result.append(command[start:index].strip())
            index += 2
            start = index
            continue
        index += 1
    result.append(command[start:].strip())
    return result


def _words(segment):
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return []


def _python(words):
    return next((i for i, word in enumerate(words)
                 if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', pathlib.PurePath(word).name)), None)


def _script_target(words):
    index = _python(words)
    if index is None:
        return None
    index += 1
    while index < len(words):
        word = words[index]
        if word in ('-c', '-m'):
            return None
        if word in ('-X', '-W', '--check-hash-based-pycs'):
            index += 2
            continue
        if word.startswith('-'):
            index += 1
            continue
        return word if word.endswith('.py') and not re.search(r'[$`~*?]', word) else None
    return None


def _setup(words):
    return bool(words) and (words[0] in ('cd', 'pushd', 'popd', 'export', 'set', 'source', '.')
                            or all(re.match(r'^\w+=', word) for word in words))


def _group(part):
    body = re.sub(r'^(?:\s*#[^\n]*\n)*\s*', '', part)
    if not body.startswith('('):
        return None
    depth, quote, index = 0, None, 0
    while index < len(body):
        char = body[index]
        if char == '\\' and quote != "'":
            index += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return body[1:index] if not body[index + 1:].strip() else None
        index += 1
    return None


def _marker(directory, index, boundary):
    key = hashlib.sha256(str(directory).encode()).hexdigest()[:24]
    return f'\x1eDOIT:{key}:{index}:{boundary}\x1f'.encode()


def shell(command, directory, baseline, *, allow_exec=True):
    """All checks run in one shell; failed cwd/env setup still stops the chain."""
    parts = split_chain(command)
    words = _words(command)
    if (allow_exec and len(parts) == 1 and _group(command) is None
            and not (baseline and (_script_target(words) or
                     ('-m' in words and words[words.index('-m') + 1:][:1] == ['unittest'])))):
        # Preserve bash's exec optimization and the child's session identity.
        return command, parts
    script = ['__doit_evidence_status=0']
    for index, part in enumerate(parts):
        words = _words(part)
        execute = part
        target = _script_target(words)
        grouped = _group(part)
        if grouped is not None:
            nested = directory / str(index)
            nested.mkdir()
            inner, _ = shell(grouped, nested, baseline, allow_exec=False)
            execute = ('(\nexport DOIT_RUNNER_REPORT=' + shlex.quote(str(nested))
                       + '\n' + inner + '\n)')
            target = None
        if baseline and target:
            quoted = shlex.quote(target)
            execute = (f'if [ -e {quoted} ]; then\n{part}\n'
                       'else\n__doit_evidence_absent=1\nfi')
        # unittest module lists otherwise fail importing a newly added module.
        if baseline and grouped is None and '-m' in words:
            module = words.index('-m')
            if (words[module:module + 2] == ['-m', 'unittest'] and
                    all(not re.search(r'[$`<>|;&()\n]', word) for word in words)):
                names = words[module + 2:]
                if names and all(re.fullmatch(r'(?:test\w*|tests|src|api)(?:\.[\w.]+)?', name) for name in names):
                    execute = ['__doit_evidence_args=()']
                    for name in names:
                        execute.append('__doit_evidence_found=0')
                        fields = name.split('.')
                        for size in range(1, len(fields) + 1):
                            prefix = '/'.join(fields[:size])
                            filename = prefix + '.py'
                            execute.append(f'if [ "$__doit_evidence_found" -eq 0 ]; then\n'
                                f'if [ -f {shlex.quote(filename)} ]; then '
                                f'__doit_evidence_args+=({shlex.quote(name)}); __doit_evidence_found=1; '
                                f'elif [ ! -d {shlex.quote(prefix)} ]; then '
                                f'printf "%s\\n" {shlex.quote(filename)} >> '
                                f'{shlex.quote(str(directory / (str(index) + ".absent")))}; '
                                '__doit_evidence_found=1; fi\nfi')
                        execute.append('if [ "$__doit_evidence_found" -eq 0 ]; then '
                                       f'__doit_evidence_args+=({shlex.quote(name)}); fi')
                    execute += ['if [ "${#__doit_evidence_args[@]}" -gt 0 ]; then',
                                shlex.join(words[:module + 2]) + ' "${__doit_evidence_args[@]}"', 'fi']
                    execute = '\n'.join(execute)
        status = shlex.quote(str(directory / f'{index}.status'))
        begin = shlex.quote(_marker(directory, index, 'begin').decode())
        end = shlex.quote(_marker(directory, index, 'end').decode())
        script += [f'export DOIT_RUNNER_STEP={index}', '__doit_evidence_absent=0',
                   f'printf %s {begin}', f'if {{\n{execute}\n}}; then',
                   '__doit_evidence_code=0', 'else', '__doit_evidence_code=$?', 'fi',
                   f'printf %s {end}',
                   f'printf "%s\\t%s\\t%s\\n" "$__doit_evidence_code" "$PWD" '
                   f'"$__doit_evidence_absent" > {status}',
                   'if [ "$__doit_evidence_status" -eq 0 ]; then '
                   '__doit_evidence_status=$__doit_evidence_code; fi']
        if _setup(words):
            script.append('if [ "$__doit_evidence_code" -ne 0 ]; then '
                          'exit "$__doit_evidence_code"; fi')
    script.append('exit "$__doit_evidence_status"')
    return '\n'.join(script), parts


def capture(directory, parts, output):
    """Frame output on its original pipe, then remove our framing byte-for-byte.

    Redirecting steps to files would let background writers lose the pipe that
    keeps them subject to _process's deadline and process-group cleanup.
    """
    cleaned = output
    for index, part in enumerate(parts):
        begin, end = (_marker(directory, index, boundary) for boundary in ('begin', 'end'))
        start = output.find(begin)
        stop = output.find(end, start + len(begin)) if start >= 0 else -1
        grouped = _group(part)
        if grouped is not None:
            cleaned = capture(directory / str(index), split_chain(grouped), cleaned)
        elif start >= 0 and stop >= 0:
            (directory / f'{index}.out').write_bytes(output[start + len(begin):stop])
        cleaned = cleaned.replace(begin, b'').replace(end, b'')
    return cleaned


def evidence(directory, parts, tree):
    records = []
    reports = [json.loads(path.read_text()) for path in directory.glob('pytest-*.json')]
    for index, part in enumerate(parts):
        status = directory / f'{index}.status'
        if not status.exists():
            continue
        grouped = _group(part)
        if grouped is not None:
            records.extend(evidence(directory / str(index), split_chain(grouped), tree))
            continue
        code, cwd, skipped = status.read_text().rstrip('\n').split('\t')
        out = directory / f'{index}.out'
        output = out.read_text(errors='replace') if out.exists() else ''
        parsed = base_red.parse_log(output)
        ids = set().union(*(value for key, value in parsed.items() if key != 'undetermined'))
        absent, results, collection_error = [], {}, False
        collected, examined = {}, set()
        target = _script_target(_words(part))
        def relative(filename):
            return str((pathlib.Path(cwd) / filename).resolve().relative_to(tree.resolve()))
        for word in _words(part):
            if not re.fullmatch(r'[\w./-]+\.(?:py|[cm]?js|tsx?|sh)(?:::.*)?', word):
                continue
            filename, *selectors = word.split('::')
            path = (pathlib.Path(cwd) / filename).resolve()
            if not path.exists() and path.is_relative_to(tree.resolve()):
                absent.append({'path': relative(filename), 'selectors': selectors})
        if skipped == '1' and target:
            absent.append({'path': relative(target), 'selectors': []})
        modules = directory / f'{index}.absent'
        if modules.exists():
            absent.extend({'path': relative(name), 'selectors': []}
                          for name in modules.read_text().splitlines())
        for report in reports:
            if report['step'] == index:
                absent.extend(report['absent'])
                for node, result in report['results'].items():
                    entry = results.setdefault(node, {'path': result['path'], 'phases': {}})
                    rank = {'passed': 0, 'skipped': 1, 'failed': 2}
                    for phase, outcome in result['phases'].items():
                        old = entry['phases'].get(phase, 'passed')
                        entry['phases'][phase] = max((old, outcome), key=rank.__getitem__)
                collection_error |= report['collection_error']
                collected.update(report.get('collected', {}))
                examined.update(report.get('examined', []))
        for node, result in results.items():
            if 'failed' in result['phases'].values():
                ids.add(node)
        # Native unittest does not print pytest-style node ids.
        for name, dotted in re.findall(r'^(?:FAIL|ERROR): (\S+) \(([^)]+)\)', output, re.M):
            if dotted.startswith('unittest.loader._FailedTest.'):
                collection_error = True
                continue
            fields = dotted.split('.')
            if len(fields) < 3:
                continue
            module = '.'.join(fields[:-2])
            filename = target if module == '__main__' else module.replace('.', '/') + '.py'
            if filename:
                node = filename + '::' + '::'.join(fields[-2:])
                ids.add(node)
                results[node] = {'path': relative(filename), 'phases': {'call': 'failed'}}
        records.append({'cmd': part, 'cwd': cwd, 'exit': int(code), 'failing_ids': sorted(ids),
                        'absent': list({(item['path'], tuple(item['selectors'])): item
                                        for item in absent}.values()), 'results': results,
                        'pytest': any(report['step'] == index for report in reports),
                        'collected': collected, 'examined': sorted(examined),
                        'uncertain': bool(parsed['undetermined']) or collection_error})
    return records


def absent(node, tip, base, tree):
    info = tip.get('results', {}).get(node)
    filename, *selectors = node.split('::')
    if info:
        path = info['path']
    else:
        try:
            path = str((pathlib.Path(tip['cwd']) / filename).resolve().relative_to(pathlib.Path(tree)))
        except ValueError:
            return False
    for missing in base.get('absent', []):
        if missing['path'] != path:
            continue
        wanted = missing['selectors']
        if all(i < len(selectors) and (part == selectors[i] or selectors[i].startswith(part + '['))
               for i, part in enumerate(wanted)):
            return True
    if path in base.get('examined', []):
        return not any(filename == path and other.split('::')[1:] == selectors
                       for other, filename in base.get('collected', {}).items())
    return False


def compare(tip, base):
    existing, new, uncertain = set(), set(), False
    attributed = set().union(*(set(step['failing_ids']) for step in tip['segments']))
    uncertain |= bool(set(tip['failing_ids']) - attributed)
    uncertain |= bool(tip['exit'] and not any(step['exit'] for step in tip['segments']))
    for index, step in enumerate(tip['segments']):
        if index >= len(base['segments']):
            uncertain |= bool(step['exit'] or step['failing_ids'])
            continue
        prior = base['segments'][index]
        uncertain |= step['uncertain'] or bool(step['exit'] and not step['failing_ids'])
        for node in step['failing_ids']:
            phases = prior.get('results', {}).get(node, {}).get('phases', {})
            if absent(node, step, prior, tip['cwd']):
                new.add(node)
            elif node in prior['failing_ids']:
                existing.add(node)
            elif (all(phases.get(phase) == 'passed' for phase in ('setup', 'call', 'teardown'))
                  or (not prior['pytest'] and not prior['uncertain']
                      and (not prior['exit'] or prior['failing_ids']))):
                new.add(node)
            else:
                uncertain = True
    return existing, new, uncertain


def compact(tip, base, invocation_id=None):
    """Publish details for failing tip nodes, rather than every passing test."""
    for index, row in enumerate(tip):
        wanted = set(row['failing_ids'])
        for record in [row] + ([base[index]] if base else []):
            for step in record.get('segments', []):
                step['n_collected'] = len(step.get('collected', {}))
                for key in ('results', 'collected'):
                    step[key] = {node: value for node, value in step.get(key, {}).items()
                                 if node in wanted}
    bundle = {"commands": tip, "base": base}
    if invocation_id is not None:
        bundle["invocation_id"] = invocation_id
    return bundle




def stamp_bundle(bundle, key):
    """Stamp publication metadata without changing collected evidence."""
    bundle['bundle_key'] = key
    for row in bundle['commands'] + bundle.get('base', []):
        row['tail_sha256'] = hashlib.sha256(pathlib.Path(row['tail']).read_bytes()).hexdigest()
