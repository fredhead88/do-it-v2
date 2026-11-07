"""Read-only Telegram caller accounting; uncertainty is a failing finding."""
import argparse
import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import stat
import subprocess
import sys
import time

import ledger_files


@dataclass(frozen=True)
class Limits:
    entries: int = 100_000
    sources: int = 25_000
    depth: int = 32
    file_bytes: int = 4 * 1024 * 1024
    total_bytes: int = 128 * 1024 * 1024
    hops: int = 16


EXCLUDED = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build',
            'docs', 'logs', 'content', 'fixtures', '__fixtures__', 'testdata',
            'test_data', 'tests', '__tests__', 'cache', 'store', 'data', 'secrets',
            '.ssh', '.aws', '.config'}
SOURCE = {'.py', '.sh', '.bash', '.js', '.mjs', '.cjs', '.ts', '.yml', '.yaml'}
KEYS = {'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'}
OPEN = ('.github/workflows/oasdiff.yml', 'infra/lambda/telegram-alerter/handler.py')
SAFE_NAME = re.compile(r'[A-Za-z0-9_.:-]{1,120}\Z')


def display(path):
    # Paths themselves are untrusted: redact token-shaped URLs and assignments.
    value = re.sub(r'https?://\S+|\b\d{6,}:[A-Za-z0-9_-]+', '[redacted]', str(path))
    return json.dumps(value, ensure_ascii=True)


def source(path):
    return (path.suffix in SOURCE or path.name in {'doit', 'Dockerfile'} or
            path.stat().st_mode & 0o111)


def excluded(path):
    return (bool(EXCLUDED.intersection(path.parts)) or path.name.startswith(('test_', '.env')) or
            path.name.endswith(('.test.ts', '.test.js', '.spec.ts')) or
            path.name in {'credentials', 'telegram.token', 'chat_id', 'config.json'})


class Incomplete(Exception):
    pass


class Scan:
    def __init__(self, doit_repo, albert_repo, root, push_dir, limits=Limits()):
        self.doit, self.albert, self.root, self.push = map(
            lambda p: Path(os.path.abspath(p)), (doit_repo, albert_repo, root, push_dir))
        self.limits = limits
        self.rows = set()
        self.failed = False
        self.files = {}
        self.canonical = self.doit / 'scripts/tg-send.sh'
        self.allowed = (self.doit, self.albert, self.root / 'bin', self.root / 'telegram', self.push)
        self.referenced = set()
        self.collecting = False
        self.stack = []
        self.visited = set()

    def report(self, kind, path, reason):
        self.rows.add(f'{kind} {display(path)}: {reason}')
        if kind in {'FAIL', 'UNKNOWN', 'INCOMPLETE'}:
            self.failed = True

    def resolve(self, path):
        # Resolve every component so links in parent directories count too.
        pending = list(Path(os.path.abspath(path)).parts[1:])
        current = Path('/')
        hops = 0
        while pending:
            piece = pending.pop(0)
            if piece == '..':
                current = current.parent
                continue
            candidate = current / piece
            if candidate.is_symlink():
                hops += 1
                if hops > self.limits.hops:
                    raise Incomplete('symlink hops limit')
                target = Path(os.readlink(candidate))
                if target.is_absolute():
                    current = Path('/')
                    pending = list(target.parts[1:]) + pending
                else:
                    pending = list(target.parts) + pending
            else:
                current = candidate
        return current

    def target(self, path):
        target = self.resolve(path)
        # Only known source areas may be read through a link. An immediate
        # root Python file is also an explicitly inspected source area.
        if excluded(Path(target.name)) or not (any(target.is_relative_to(p) and not excluded(target.relative_to(p))
                    for p in self.allowed) or
                target.parent == self.root and target.suffix == '.py'):
            raise Incomplete('symlink target outside inspected source areas')
        if not target.is_file() or not source(target):
            raise Incomplete('missing or non-source symlink target')
        return target

    def discover(self, root, mode='tree'):
        self.report('ROOT', root, 'source inspection')
        count = size = files = 0
        seen = set()

        def children(directory):
            nonlocal count
            names = []
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > self.limits.entries:
                        raise Incomplete('directory entries limit')
                    names.append(entry.name)
            return sorted(names)

        def visit(path, depth):
            nonlocal size, files
            if depth > self.limits.depth:
                raise Incomplete('relative depth limit')
            rel = path.relative_to(root)
            if excluded(rel):
                self.report('EXCLUDED', path, 'documentation, tests, generated, credentials or client data')
                return
            if path.is_symlink():
                target = self.target(path)
            else:
                target = path
            st = target.stat()
            if not st.st_mode & 0o444:
                raise Incomplete('unreadable source or directory')
            if stat.S_ISDIR(st.st_mode):
                if mode == 'immediate':
                    return
                if mode == 'push' and os.path.lexists(target / '.git'):
                    self.report('EXCLUDED', path, 'nested Git checkout; active references still inspected')
                    return
                if target in seen:
                    raise Incomplete('directory cycle')
                seen.add(target)
                for name in children(target):
                    visit(path / name, depth + 1)
                return
            if not stat.S_ISREG(st.st_mode):
                raise Incomplete('unsupported filesystem entry')
            if mode == 'immediate' and path.suffix != '.py':
                return
            if not source(path) and not path.is_symlink():
                return
            files += 1
            size += st.st_size
            if files > self.limits.sources:
                raise Incomplete('source files limit')
            if st.st_size > self.limits.file_bytes:
                raise Incomplete('per-file bytes limit')
            if size > self.limits.total_bytes:
                raise Incomplete('cumulative source bytes limit')
            with target.open('rb') as stream:
                data = stream.read(self.limits.file_bytes + 1)
            if len(data) != st.st_size:
                raise Incomplete('source changed during inspection')
            self.files[path] = (target, data.decode('utf-8'))

        try:
            if not root.is_dir():
                raise Incomplete('required root missing')
            for name in children(root):
                visit(root / name, 1)
        except (OSError, UnicodeError, Incomplete):
            # Exception text may contain source bytes or credentials.
            reason = sys.exc_info()[1]
            self.report('INCOMPLETE', root, str(reason) if isinstance(reason, Incomplete)
                        else 'unreadable source or traversal')

    def inbound(self, path, target):
        versioned = self.doit / 'scripts/tg-poll.py'
        return (path == versioned or path == self.root / 'telegram/tg-poll.py' and
                (path == target or target == versioned))

    def transport(self, path, target, text):
        if target == self.canonical:
            self.report('PASS', path, 'canonical outbound transport')
            return
        poller = self.inbound(path, target)
        # Resolve the poller helper's method at each actual call site below.
        if path.suffix == '.py':
            try:
                tree = ast.parse(text)
            except SyntaxError:
                self.report('UNKNOWN', path, 'Python source cannot be parsed')
                return
            self.python(path, tree, poller)
            # URL literals are evidence only when used by a call. Follow static
            # assignments without treating diagnostic tables/docstrings as senders.
            assignments = {target.id: node.value for node in ast.walk(tree)
                           if isinstance(node, ast.Assign) for target in node.targets
                           if isinstance(target, ast.Name)}

            def strings_used(expression):
                strings, visited = set(), set()
                def visit(node, depth=0):
                    if id(node) in visited or depth > 16:
                        return
                    visited.add(id(node))
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        strings.add(node.value)
                    if isinstance(node, ast.Name) and node.id in assignments:
                        visit(assignments[node.id], depth + 1)
                    for child in ast.iter_child_nodes(node):
                        visit(child, depth)
                visit(expression)
                return '\n'.join(sorted(strings))

            urls = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                called = ast.unparse(node.func).split('.')[-1]
                if called in {'print', 'repr', 'str', 'report', 'info', 'debug', 'warning', 'error',
                              'search', 'match', 'fullmatch', 'findall', 'sub', 'compile'}:
                    continue
                arguments = '\n'.join(strings_used(arg) for arg in
                                      [*node.args, *(kw.value for kw in node.keywords)])
                if 'api.telegram.org' in arguments or poller and called == 'api':
                    urls.append(arguments)
            urls = '\n'.join(urls)
            if poller:
                parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
                helper_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                                and isinstance(node.func, ast.Name) and node.func.id == 'api']
                if not helper_calls:
                    self.report('UNKNOWN', path, 'poller helper has no resolved inbound call sites')
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.Constant, ast.JoinedStr)):
                        continue
                    if isinstance(node, ast.Constant) and isinstance(parents.get(node), ast.JoinedStr):
                        continue
                    rendered = ast.unparse(node)
                    if 'api.telegram.org' not in rendered:
                        continue
                    owner = node
                    while owner in parents and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        owner = parents[owner]
                    endpoint = (isinstance(node, ast.JoinedStr) and len(node.values) == 4 and
                                isinstance(node.values[0], ast.Constant) and
                                node.values[0].value == 'https://api.telegram.org/bot' and
                                isinstance(node.values[1], ast.FormattedValue) and
                                isinstance(node.values[1].value, ast.Name) and node.values[1].value.id == 'tok' and
                                isinstance(node.values[2], ast.Constant) and node.values[2].value == '/' and
                                isinstance(node.values[3], ast.FormattedValue) and
                                isinstance(node.values[3].value, ast.Name) and node.values[3].value.id == 'method' and
                                isinstance(owner, ast.FunctionDef) and owner.name == 'api' and
                                [a.arg for a in owner.args.args][:2] == ['tok', 'method'])
                    download = (isinstance(node, ast.JoinedStr) and node.values and
                                isinstance(node.values[0], ast.Constant) and
                                node.values[0].value == 'https://api.telegram.org/file/bot' and
                                any(len(c.args) > 1 and isinstance(c.args[1], ast.Constant) and
                                    c.args[1].value == 'getFile' for c in helper_calls))
                    if not endpoint and not download:
                        self.report('FAIL', path, 'poller has an unsupported Telegram endpoint')
        else:
            urls = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))
            if path not in self.referenced or self.collecting:
                self.shell(path, text)
        if 'api.telegram.org' not in urls and not re.search(r'\b(?:sendMessage|sendDocument)\b', urls):
            return
        methods = re.findall(r'\b(?:sendMessage|sendDocument|getUpdates|getFile|getChat)\b', urls)
        exception = path in {self.albert / item for item in OPEN}
        if exception and methods == ['sendMessage']:
            self.report('OPEN', path, 'pending off-box sendMessage route')
            return
        if poller and 'bot' in urls:
            # The only dynamic endpoint allowed is the api(tok, method) helper;
            # any literal outbound/other method remains a violation.
            if any(m not in {'getUpdates', 'getFile'} for m in methods):
                self.report('FAIL', path, 'poller contains forbidden Telegram method')
            elif '/file/bot' in urls and 'getFile' not in text:
                self.report('FAIL', path, 'download without getFile route')
            else:
                self.report('PASS', path, 'inbound URL route; helper calls checked separately')
            return
        self.report('FAIL', path, 'direct or unresolved Telegram API outside canonical transport')

    def attribution(self, path, route, sender, env=None):
        env = env or {}
        if not isinstance(sender, str) or not SAFE_NAME.fullmatch(sender):
            self.report('UNKNOWN', path, route + ' has missing or dynamic sender')
        elif env.get('TG_CLASS') == 'reply' and not re.fullmatch(r'[1-9][0-9]*', str(env.get('TG_REPLY_TO', ''))):
            self.report('UNKNOWN', path, route + ' reply lacks TG_REPLY_TO forwarding')
        elif not isinstance(env.get('TG_CLASS', 'automated'), str) or env.get('TG_CLASS', 'automated') not in {'automated', 'reply'}:
            self.report('UNKNOWN', path, route + ' has dynamic reply class')
        else:
            self.report('PASS', path, route + ' sender=' + display(sender))

    def route(self, path, words, env):
        if not words:
            return
        if any(not isinstance(word, str) for word in words):
            self.report('UNKNOWN', path, 'dynamic subprocess command arguments')
            return
        words = list(words)
        while words and words[0] in {'exec', 'command', 'bash', 'sh', 'python', 'python3', 'env'}:
            words.pop(0)
        if not words:
            return
        executable = str(words[0]).replace('${DOIT_ROOT}', str(self.root)).replace('$DOIT_ROOT', str(self.root))
        candidate_path = Path(executable) if executable.startswith('/') else path.parent / executable
        # Every explicit script delegation is checked, including arbitrary
        # wrapper names and active references into pruned checkouts.
        if '$' not in executable and ('/' in executable or Path(executable).suffix in {'.sh', '.py'}):
            try:
                dest = self.target(candidate_path)
                entry = next(((p, text) for p, (target, text) in self.files.items() if target == dest), None)
                if dest != self.canonical and entry and dest.suffix == '.sh':
                    wrapper, body = entry
                    self.referenced.add(wrapper)
                    if self.collecting:
                        return
                    if dest in self.stack or len(self.stack) >= self.limits.hops:
                        self.report('UNKNOWN', path, 'cyclic or excessive wrapper delegation')
                        return
                    self.stack.append(dest)
                    self.visited.add(wrapper)
                    prior = self.failed
                    try:
                        self.shell(wrapper, body, env)
                    finally:
                        self.stack.pop()
                    self.report('UNKNOWN' if self.failed and not prior else 'ROUTE', path,
                                'delegated script ' + display(wrapper) + '; inherited attribution checked')
                    return
                if not entry and dest != self.canonical:
                    self.report('UNKNOWN', path, 'script route outside enumerated source inventory')
            except (OSError, Incomplete):
                if executable.endswith(('.sh', '.py')):
                    self.report('UNKNOWN', path, 'unresolved delegated script route')
        joined = ' '.join(str(w) for w in words)
        if 'tg-send' in joined:
            # The command must identify the canonical sender, never a same-name
            # regular copy. Dynamic paths remain unresolved unless rooted in the
            # explicit inspected live bin or repository scripts directories.
            candidate = next((str(w) for w in words if 'tg-send' in str(w)), '')
            candidate = candidate.replace('${DOIT_ROOT}', str(self.root)).replace('$DOIT_ROOT', str(self.root))
            if '$' in candidate or candidate in {'tg-send.sh', 'tg-send-file.sh'}:
                self.report('UNKNOWN', path, 'unresolved sender executable route')
            else:
                dest = Path(candidate) if candidate.startswith('/') else path.parent / candidate
                try:
                    target = self.target(dest)
                    if target != self.canonical:
                        self.report('FAIL', path, 'sender route targets independent transport or wrapper')
                except (OSError, Incomplete):
                    self.report('UNKNOWN', path, 'unresolved sender executable route')
            self.attribution(path, 'tg-send invocation', env.get('TG_SENDER'), env)
        elif 'tg-say' in words:
            sender = None
            if '--sender' in words:
                i = words.index('--sender')
                sender = words[i + 1] if i + 1 < len(words) else None
            self.attribution(path, 'doit tg-say', sender)

    def shell(self, path, text, inherited=None):
        exported = dict(inherited or {})
        conditional = 0
        for line in text.replace('\\\n', ' ').splitlines():
            try:
                lexer = shlex.shlex(line, posix=True, punctuation_chars=';&|()')
                lexer.whitespace_split = True
                tokens = list(lexer)
            except ValueError:
                self.report('UNKNOWN', path, 'unparseable shell source')
                continue
            chunks = [[]]
            for token in tokens:
                if token in {';', '&&', '||', '|', '(', ')', '&'}:
                    chunks.append([])
                else:
                    chunks[-1].append(token)
            for words in chunks:
                if words and words[0] in {'if', 'for', 'while', 'case', 'function'}:
                    conditional += 1
                if words and words[0] in {'fi', 'done', 'esac', '}'}:
                    conditional = max(0, conditional - 1)
                env = dict(exported)
                command = []
                export = bool(words and words[0] == 'export')
                for token in words[1:] if export else words:
                    match = re.fullmatch(r'(TG_SENDER|TG_CLASS|TG_REPLY_TO)=(.*)', token)
                    if match and not command:
                        value = match[2]
                        for key, val in exported.items():
                            if isinstance(val, str):
                                value = value.replace('${' + key + '}', val).replace('$' + key, val)
                        env[match[1]] = value
                    else:
                        command.append(token)
                if export:
                    exported = {k: (None if conditional else v) for k, v in env.items()}
                self.route(path, command, env)

    def python(self, path, tree, poller):
        bindings = {}
        aliases = {'tg_say.deliver'}
        subprocess_aliases = set()
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        nested = False

        def value(node):
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name):
                return None if nested else bindings.get(node.id)
            if isinstance(node, ast.Dict):
                return {value(k): value(v) for k, v in zip(node.keys, node.values) if k}
            if isinstance(node, (ast.List, ast.Tuple)):
                return [value(n) for n in node.elts]
            return None

        def name(node):
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, ast.Attribute):
                return name(node.value) + '.' + node.attr
            return ''

        module_assignments = {id(n) for n in tree.body if isinstance(n, ast.Assign)}
        # Imports establish names, but assignments are applied in source order.
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == 'tg_say':
                aliases.update(n.asname or n.name for n in node.names if n.name == 'deliver')
            if isinstance(node, ast.Import):
                aliases.update((n.asname or n.name) + '.deliver' for n in node.names if n.name == 'tg_say')
            if isinstance(node, ast.ImportFrom) and node.module == 'subprocess':
                subprocess_aliases.update(n.asname or n.name for n in node.names
                                          if n.name in {'run', 'Popen', 'call', 'check_call', 'check_output'})
        for node in sorted(ast.walk(tree), key=lambda n: (getattr(n, 'lineno', 0), getattr(n, 'col_offset', 0))):
            owner = node
            nested = False
            while owner in parents:
                owner = parents[owner]
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                    nested = True
                    break
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bindings[target.id] = value(node.value) if id(node) in module_assignments else None
            if not isinstance(node, ast.Call):
                continue
            called = name(node.func)
            kwargs = {n.arg: value(n.value) for n in node.keywords}
            if called in aliases:
                self.attribution(path, 'tg_say.deliver', value(node.args[0]) if node.args else kwargs.get('sender'))
            if called == 'api' and poller:
                method = value(node.args[1]) if len(node.args) > 1 else kwargs.get('method')
                method_node = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == 'method'), None)
                if isinstance(method, str) and method in {'getUpdates', 'getFile'} and isinstance(method_node, ast.Constant):
                    self.report('PASS', path, 'inbound ' + method)
                else:
                    self.report('UNKNOWN' if not isinstance(method, str) else 'FAIL', path,
                                'unresolved or forbidden poller helper method')
            if (called in subprocess_aliases or called.split('.')[-1] in
                    {'run', 'Popen', 'call', 'check_call', 'check_output', 'system'}) and node.args:
                argv = value(node.args[0])
                if isinstance(argv, str):
                    self.shell(path, argv, kwargs.get('env') if isinstance(kwargs.get('env'), dict) else {})
                elif isinstance(argv, list):
                    self.route(path, argv, kwargs.get('env') if isinstance(kwargs.get('env'), dict) else {})
                elif 'tg-send' in ast.unparse(node) or 'tg-say' in ast.unparse(node):
                    self.report('UNKNOWN', path, 'dynamic subprocess sender invocation')

    def run(self):
        for root, mode in ((self.doit, 'tree'), (self.albert, 'tree'),
                           (self.root / 'bin', 'tree'), (self.root / 'telegram', 'tree'),
                           (self.root, 'immediate'), (self.push, 'push')):
            self.discover(root, mode)
        # Find wrapper entry points before assessing their inherited environment.
        # Discovery findings survive this pass; provisional attribution does not.
        saved, failed = self.rows.copy(), self.failed
        self.collecting = True
        for path, (target, text) in sorted(self.files.items()):
            self.transport(path, target, text)
        self.rows, self.failed = saved, failed
        self.collecting = False
        for path, (target, text) in sorted(self.files.items()):
            self.transport(path, target, text)
        for path in sorted(self.referenced - self.visited):
            self.shell(path, self.files[path][1])
        for item in OPEN:
            self.report('OPEN', self.albert / item, 'deployment and credentials require off-box evidence')
        return not self.failed


def collect(argv, timeout=30, cap=1024 * 1024):
    """Bound both pipes while running; never return remote error bytes."""
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL) as proc:
        selector = selectors.DefaultSelector()
        try:
            selector.register(proc.stdout, selectors.EVENT_READ)
            selector.register(proc.stderr, selectors.EVENT_READ)
            data, total, deadline = bytearray(), 0, time.monotonic() + timeout
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Incomplete('external evidence timeout')
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > cap:
                        raise Incomplete('external evidence output limit')
                    if key.fileobj is proc.stdout:
                        data.extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or proc.wait(timeout=remaining) != 0:
                raise Incomplete('external evidence unavailable')
            return bytes(data).decode('utf-8')
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            selector.close()


# The .env is read only by the remote process. Its stdout consists exclusively
# of key names, including keys whose values are empty. It is never sourced.
REMOTE = """import pathlib,re,json
p=pathlib.Path('/opt/albert-scott/.env')
keys=[]
for line in p.read_text().splitlines():
    line=line.strip()
    if not line or line.startswith('#'): continue
    m=re.match(r'(?:export\\s+)?([A-Za-z_][A-Za-z0-9_]*)\\s*=',line)
    if not m: raise SystemExit(1)
    keys.append(m[1])
print(json.dumps(sorted(set(keys))))
"""


def instant(value, now):
    if not isinstance(value, str):
        raise ValueError
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None or result > now:
        raise ValueError
    return result


def reason(event):
    why = event.get('why')
    return json.loads(why) if isinstance(why, str) else why


def silence(event, now):
    why = reason(event)
    if not isinstance(why, dict) or why.get('resource') != 'telegram-alerter':
        raise ValueError
    state = why.get('state')
    if state not in {'not-deployed', 'disabled'} or state == 'disabled' and why.get('can_send') is not False:
        raise ValueError
    instant(event.get('ts'), now)
    instant(why.get('observed_at'), now)
    if not isinstance(why.get('evidence_ref'), str) or not why['evidence_ref'].strip():
        raise ValueError
    return state


def lambda_silent(root, ref, now=None):
    from fold import actor_of  # Pure filename authority only; no cached readers.
    now = now or datetime.now(timezone.utc)
    try:
        match = re.fullmatch(r'([^/\\:]+\.jsonl):([1-9][0-9]*)', ref or '')
        if not match or '..' in match[1]:
            return False
        directory = Path(root) / 'events'
        if directory.is_symlink():
            return False
        ledger_files.assert_readable(directory)
        # Do not let virtual source reads follow links outside the fixture root.
        def inaccessible(error):
            raise error
        for base, dirs, files in os.walk(directory, followlinks=False, onerror=inaccessible):
            if any((Path(base) / name).is_symlink() for name in dirs + files):
                return False
        if ledger_files.problems(directory):
            return False
        segments = directory / 'segments'
        if any(not p.with_suffix('.idx.json').is_file() for p in segments.glob('*.jsonl')):
            return False
        # Validate segment integrity without verify_segments' state-cache write.
        for index in (directory / 'segments').glob('*.idx.json'):
            obj = json.loads(index.read_text())
            data = (index.parent / obj['segment']).read_bytes()
            if hashlib.sha256(data).hexdigest() != obj['sha256']:
                return False
            for member in obj['members']:
                part = data[member['offset']:member['offset'] + member['length']]
                if hashlib.sha256(part).hexdigest() != member['sha256']:
                    return False
        name, row = match[1], int(match[2])
        selected = json.loads(ledger_files.read_text(directory, name).splitlines()[row - 1])
        if (actor_of(name) not in {'operator', 'thinker'} or not isinstance(selected, dict) or
                selected.get('type') != 'decision' or selected.get('subject') != 'L-charter-0085'):
            return False
        state = silence(selected, now)
        chosen_time = instant(selected.get('ts'), now)
        for filename, lines in ledger_files.iter_sources(directory):
            for number, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    return False
                if (actor_of(filename) not in {'operator', 'thinker'} or event.get('type') != 'decision' or
                        event.get('subject') != 'L-charter-0085'):
                    continue
                try:
                    why = reason(event)
                except (ValueError, TypeError):
                    why = None
                if not (isinstance(why, dict) and why.get('resource') == 'telegram-alerter' or
                        not isinstance(why, dict) and 'telegram-alerter' in str(event.get('why', ''))):
                    continue
                when = instant(event.get('ts'), now)
                later = when > chosen_time or when == chosen_time and (filename != name or number > row)
                if later and silence(event, now) != state:
                    return False
        return True
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError):
        return False


def offbox(scan, ref):
    commands = (
        ('GitHub repository secret names', ['gh', 'secret', 'list', '--repo',
                                           'fredhead88/albert-scott-platform', '--json', 'name']),
        ('production environment key names', ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=30',
                                             'root@167.71.46.51', 'python3 -c ' + shlex.quote(REMOTE)]))
    for label, argv in commands:
        try:
            result = json.loads(collect(argv))
            if not isinstance(result, list):
                raise ValueError
            if argv[0] == 'gh':
                if any(not isinstance(item, dict) or set(item) != {'name'} for item in result):
                    raise ValueError
                result = [item['name'] for item in result]
            if any(not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) for key in result):
                raise ValueError
            present = KEYS.intersection(result)
            scan.report('FAIL' if present else 'PASS', label,
                        'Telegram key names present' if present else 'neither Telegram key name present')
        except (OSError, ValueError, UnicodeError, Incomplete, subprocess.TimeoutExpired):
            scan.report('UNKNOWN', label, 'key-name collection unavailable or incomplete')
    scan.report('PASS' if lambda_silent(scan.root, ref) else 'UNKNOWN', 'telegram-alerter',
                'Lambda decision evidence')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'Local mode accounts for local callers only. --offbox also requires secret-name '
        'absence and affirmative Lambda silence evidence. Root flags support isolated fixtures.'))
    parser.add_argument('--doit-repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--albert-repo', type=Path, default=Path.home() / '.do-it/repos/albert-scott')
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('DOIT_ROOT', Path.home() / '.do-it')))
    parser.add_argument('--push-dir', type=Path, default=Path('/var/tmp/push'))
    parser.add_argument('--offbox', action='store_true')
    parser.add_argument('--lambda-decision-ref')
    args = parser.parse_args(argv)
    if args.lambda_decision_ref and not args.offbox:
        parser.error('--lambda-decision-ref requires --offbox')
    scan = Scan(args.doit_repo, args.albert_repo, args.root, args.push_dir)
    scan.run()
    if args.offbox:
        offbox(scan, args.lambda_decision_ref)
    for row in sorted(scan.rows):
        print(row)
    if not args.offbox:
        print('Off-box silence was not checked. Verdict covers enumerated local callers only.')
    print('FAIL: violations, unknown evidence or incomplete scan.' if scan.failed else
          'PASS: enumerated callers accounted for' + ('; off-box evidence confirmed.' if args.offbox else '.'))
    return int(scan.failed)


if __name__ == '__main__':
    sys.exit(main())
