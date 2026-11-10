#!/usr/bin/env python3
"""Explicit, reversible Telegram activation. No runtime effects on import."""
import argparse
import ast
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
LINKS = {
    'root/telegram/tg-send.sh': 'scripts/tg-send.sh',
    'root/telegram/tg-send-file.sh': 'scripts/tg-send-file.sh',
    'root/telegram/tg-poll.py': 'scripts/tg-poll.py',
    'root/telegram/pulse.py': 'scripts/pulse.py',
    'root/bin/claude-autoswitch.py': 'scripts/claude-autoswitch.py',
    'root/thinker-inbox.py': 'src/thinker_inbox.py',
}
PATCHES = ('push/rloop.sh', 'root/bin/swap-watch.sh',
           'push/foreman/catcher/catcher.py', 'push/hetzner/watch.py')
LEGACY = ('root/bin/gh-bill-check.py', 'root/bin/push-babysit.sh')
MARKER = 'root/state/tg-cutover-done'
RETAINED = ('root/telegram/tg-gate.py', 'root/telegram/tg-daily.py')
TARGETS = tuple(LINKS) + PATCHES + LEGACY + RETAINED + (MARKER,)
STOPGAP = re.compile(r'(?<![\w-])tg-(?:gate|daily)\.py(?=\s|$)')


class Failure(Exception):
    """Only fixed, content-free diagnostics are allowed to escape."""


def source_identity(path):
    resolved = path.resolve(strict=True)
    st = resolved.stat()
    return str(resolved), hashlib.sha256(resolved.read_bytes()).hexdigest()+':'+str(st.st_dev)+':'+str(st.st_ino)+':'+str(st.st_mtime_ns)


def active(line):
    return bool(line.strip()) and not line.lstrip().startswith('#')


def atomic(path, data, mode=0o600):
    """Replace the directory entry, never write through a destination link."""
    fd, name = tempfile.mkstemp(prefix='.tg-cutover-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.lexists(name):
            os.unlink(name)


class OS:
    """External boundaries only; tests replace these without replacing writes."""
    now = staticmethod(time.time)
    sleep = staticmethod(time.sleep)

    def cron_read(self):
        p = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        if p.returncode and 'no crontab' not in p.stderr.lower():
            raise Failure('crontab read failed')
        return p.stdout if not p.returncode else ''

    def cron_write(self, text):
        if subprocess.run(['crontab', '-'], input=text, text=True, capture_output=True).returncode:
            raise Failure('crontab write failed')

    def helper(self, push):
        return subprocess.run([str(push/'restart-driver.sh')], cwd=push,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode

    def gate(self, repo):
        # This diagnostic is the checker's public output, passed through intact.
        return subprocess.run([str(repo/'doit'), 'tg-check']).returncode

    def manifest(self, repo):
        import crons
        return crons.rows(repo/'crons.toml')

    def processes(self):
        ticks = os.sysconf('SC_CLK_TCK')
        boot = time.time()-time.clock_gettime(time.CLOCK_BOOTTIME)
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                fields = (entry/'stat').read_text().rsplit(')', 1)[1].split()
                if fields[0] == 'Z':
                    continue
                argv = (entry/'cmdline').read_bytes().decode().strip('\0').split('\0')
                yield entry, argv, fields[19], boot+int(fields[19])/ticks
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, UnicodeError):
                raise Failure('process discovery inaccessible') from None

    def driver_running(self, push):
        expected = push/'rloop.run.sh'
        for entry, argv, generation, started in self.processes():
            if not argv or Path(argv[0]).name not in ('bash', 'sh'):
                continue
            args = [arg for arg in argv[1:] if arg not in ('-e', '-u', '-x', '-eu', '-ue')]
            for arg in args[:1]:
                if Path(arg).name == expected.name:
                    candidate = Path(arg) if Path(arg).is_absolute() else (entry/'cwd').resolve()/arg
                    if candidate.resolve() == expected.resolve():
                        return True
        return False

    def pollers(self, path):
        result = []
        for entry, argv, generation, started in self.processes():
            matches = [s for s in argv[1:] if Path(s).name == 'tg-poll.py']
            if not matches:
                continue
            scripts = argv[1:]
            while scripts and scripts[0] in ('-u', '-B', '-E', '-s', '-I'):
                scripts = scripts[1:]
            if len(matches) != 1 or not re.fullmatch(r'python(?:3(?:\.\d+)?)?', Path(argv[0]).name) or scripts != matches:
                raise Failure('poller ownership ambiguous')
            arg = Path(matches[0])
            script = arg if arg.is_absolute() else (entry/'cwd').resolve()/arg
            if os.path.abspath(script) != str(path) and script.resolve() != path.resolve():
                raise Failure('poller ownership ambiguous')
            resolved, identity = source_identity(script)
            # Content/path may only be associated with a start if both predate it.
            # A replaced symlink's ctime disqualifies a still-running old process.
            stable_since = max(script.lstat().st_ctime, script.resolve().stat().st_ctime)
            result.append(dict(pid=int(entry.name), generation=generation, started=started, argv=argv,
                               source=resolved, identity=identity if stable_since < started else None))
        if len(result) > 1:
            raise Failure('poller ownership ambiguous')
        return result

    def stop(self, proc):
        pid = proc['pid']
        try:
            # Pin the task with pidfd: a reused numeric PID must not be signalled.
            fd = os.pidfd_open(pid)
            try:
                current = Path('/proc')/str(pid)/'stat'
                generation = current.read_text().rsplit(')', 1)[1].split()[19]
                argv = (current.parent/'cmdline').read_bytes().decode().strip('\0').split('\0')
                if generation != proc['generation'] or argv != proc['argv']:
                    raise Failure('poller generation/ownership changed before stop')
                signal.pidfd_send_signal(fd, signal.SIGTERM)
            finally:
                os.close(fd)
        except ProcessLookupError:
            pass


class Cutover:
    def __init__(self, root, push, repo=REPO, boundary=None):
        self.root, self.push, self.repo = [Path(os.path.abspath(p)) for p in (root, push, repo)]
        self.os = boundary or OS()
        self.state = self.root/'state/telegram-cutover'
        self.stage = 'planning'
        self.record = None

    def safe(self, path):
        root = self.root if path.is_relative_to(self.root) else self.push if path.is_relative_to(self.push) else None
        if root is None:
            raise Failure('destination outside inventory')
        for parent in (path.parent, *path.parent.parents):
            if parent.is_symlink():
                raise Failure('unexpected symlink parent')
        if path.is_dir() and not path.is_symlink():
            raise Failure('destination is a directory')
        return path

    def path(self, key):
        if key not in TARGETS and key != 'push/rloop.run.sh':
            raise Failure('backup destination outside inventory')
        prefix, relative = key.split('/', 1)
        return self.safe((self.root if prefix == 'root' else self.push)/relative)

    def snapshot(self, path):
        self.safe(path)
        if path.is_symlink():
            return dict(kind='link', target=os.readlink(path))
        if not path.exists():
            return dict(kind='missing')
        if not path.is_file():
            raise Failure('unsupported destination')
        return dict(kind='file', data=base64.b64encode(path.read_bytes()).decode(), mode=stat.S_IMODE(path.stat().st_mode))

    def restore(self, path, image):
        self.safe(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if image['kind'] == 'file':
            atomic(path, base64.b64decode(image['data'], validate=True), image['mode'])
        elif image['kind'] == 'link':
            fd, name = tempfile.mkstemp(prefix='.tg-cutover-', dir=path.parent)
            os.close(fd)
            os.unlink(name)
            try:
                os.symlink(image['target'], name)
                os.replace(name, path)
            finally:
                if os.path.lexists(name):
                    os.unlink(name)
        elif image['kind'] == 'missing':
            path.unlink(missing_ok=True)
        else:
            raise Failure('invalid pre-image kind')

    def save(self):
        atomic(self.state/'record.json', json.dumps(self.record, sort_keys=True).encode())

    def load(self):
        p = self.state/'record.json'
        self.safe(p)
        if not p.is_file() or p.is_symlink() or stat.S_IMODE(p.stat().st_mode) & 0o077:
            raise Failure('missing or unsafe backup')
        data = json.loads(p.read_text())
        if data.get('version') != 1 or data.get('root') != str(self.root) or data.get('push') != str(self.push):
            raise Failure('backup roots/version mismatch')
        if set(data.get('images', {})) != set(TARGETS):
            raise Failure('backup destination outside inventory')
        if not isinstance(data.get('cron'), str):
            raise Failure('invalid crontab pre-image')
        if not isinstance(data.get('touched'), list) or any(k not in TARGETS for k in data['touched']):
            raise Failure('invalid touched inventory')
        for key, image in data['images'].items():
            self.path(key)
            if image.get('kind') == 'file':
                base64.b64decode(image['data'], validate=True)
                if type(image.get('mode')) is not int or not 0 <= image['mode'] <= 0o777:
                    raise Failure('invalid pre-image mode')
            elif image.get('kind') == 'link':
                if not isinstance(image.get('target'), str):
                    raise Failure('invalid pre-image link')
            elif image.get('kind') != 'missing':
                raise Failure('invalid pre-image kind')
        if data['images']['push/rloop.sh']['kind'] != 'file':
            raise Failure('loop pre-image must be regular file')
        self.record = data

    def text(self, key):
        path = self.path(key)
        # Source patches can read only recorded inventory objects, never external links.
        if path.is_symlink():
            raise Failure('patch source must be regular file')
        return path.read_text()

    def patches(self):
        result = {}
        loop = self.text('push/rloop.sh')
        command = shlex.quote(str(self.repo/'doit'))+' merge-news record "$*"'
        pattern = re.compile(r'^tg\s*\(\)\s*\{[^\n]*\}[^\n]*$', re.M)
        matches = list(pattern.finditer(loop))
        if len(matches) != 1 or not ('tg-send' in matches[0].group() or command in matches[0].group()):
            raise Failure('loop tg patch ambiguous')
        # Remove the legacy per-message suppressor together with the direct send.
        replacement = 'tg(){ log "TG $*"; '+command+'; }'
        result['push/rloop.sh'] = pattern.sub(lambda _: replacement, loop)
        swap = self.text('root/bin/swap-watch.sh')
        lines = swap.splitlines(keepends=True)
        sends = [i for i, line in enumerate(lines) if 'tg-send.sh' in line and active(line)]
        if len(sends) == 1:
            i = sends[0]
            # Supported live shape is cleanup bookkeeping followed by one alert expression.
            m = re.fullmatch(r'(\s*touch\s+[^;\n]+;)\s*\[[^\n]+\]\s*&&\s*\S*tg-send\.sh\s+[^\n]+\|\|\s*true\s*\n?', lines[i])
            if not m:
                raise Failure('swap alert patch ambiguous')
            lines[i] = m.group(1)+' # telegram-cutover: cleanup only\n'
        elif not sends and '# telegram-cutover: cleanup only' in swap:
            pass
        else:
            raise Failure('swap alert patch ambiguous')
        result['root/bin/swap-watch.sh'] = ''.join(lines)
        for key, sender, labels in [('push/foreman/catcher/catcher.py', 'catcher', ('[DOWN]', '[DAILY]')),
                                     ('push/hetzner/watch.py', 'hetzner-watch', ('[DAILY]',))]:
            source = self.text(key)
            tree = ast.parse(source)
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and
                     isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and
                     n.func.value.id == 'subprocess' and n.func.attr == 'run' and n.args and
                     isinstance(n.args[0], ast.List) and n.args[0].elts and
                     isinstance(n.args[0].elts[0], ast.Name) and n.args[0].elts[0].id == 'TG']
            calls.sort(key=lambda n: n.lineno)
            if len(calls) != len(labels):
                # An installed file is accepted only by exact recorded installed identity.
                installed = self.record.get('installed', {}) if self.record else {}
                if installed.get(key) == hashlib.sha256(source.encode()).hexdigest():
                    result[key] = source
                    continue
                raise Failure('notification patch ambiguous')
            lines = source.splitlines(keepends=True)
            offsets = [0]
            for line in lines:
                offsets.append(offsets[-1]+len(line))
            edits = []
            for call, label in zip(calls, labels):
                vector = call.args[0]
                if len(vector.elts) != 2 or any(k.arg not in ('capture_output', 'text') for k in call.keywords):
                    raise Failure('notification arguments ambiguous')
                message = ast.get_source_segment(source, vector.elts[1])
                replacement = ('['+repr(str(self.repo/'doit'))+", 'tg-say', '--sender', "+repr(sender)+
                               ", '--label', "+repr(label)+", '--what', 'Service notification', '--real', "+message+
                               ", '--you', 'No action required']")
                # AST columns are UTF-8 byte offsets; convert before indexing text.
                start = offsets[vector.lineno-1]+len(lines[vector.lineno-1].encode()[:vector.col_offset].decode())
                end = offsets[vector.end_lineno-1]+len(lines[vector.end_lineno-1].encode()[:vector.end_col_offset].decode())
                edits.append((start, end, replacement))
            for start, end, replacement in reversed(edits):
                source = source[:start]+replacement+source[end:]
            ast.parse(source)
            result[key] = source
        return result

    def cron_install(self, current):
        rows = self.os.manifest(self.repo)
        wanted = [r for r in rows if r['where'] == 'user' and
                  (r['name'] in ('thinker-inbox', 'merge-news') or 'daily_summary.py' in r['command'])]
        if len(wanted) != 3 or len({r['name'] for r in wanted}) != 3:
            raise Failure('full-wave inbox/merge-news/daily manifest required')
        lines = current.splitlines()
        for row in wanted:
            tag = '# doit-cron:'+row['name']
            # Redirect a legacy inbox in place, preserving its cadence and wrappers otherwise.
            indices = [i for i, line in enumerate(lines) if active(line) and
                       (tag in line or (row['name'] == 'thinker-inbox' and re.search(r'(?:thinker-inbox|thinker_inbox)\.py\b', line)) or
                        row['command'] in line)]
            if len(indices) > 1:
                raise Failure('duplicate producer cron invocation')
            if indices:
                i = indices[0]
                if row['name'] == 'thinker-inbox' and re.search(r'\S*thinker-inbox\.py\b', lines[i]):
                    lines[i] = re.sub(r'\S*thinker-inbox\.py\b', str(self.repo/'src/thinker_inbox.py'), lines[i])
            else:
                lines.append(row['schedule']+' '+row['command']+' '+tag)
        return '\n'.join(lines)+'\n'

    def loop_step(self, desired):
        self.stage = 'loop restart'
        path = self.path('push/rloop.sh')
        before = self.snapshot(path)
        self.record['loop_attempt'] = before
        self.save()
        try:
            self.restore(path, desired)
            code = self.os.helper(self.push)
            if code or (self.push/'rloop.run.sh').read_bytes() != path.read_bytes() or not self.os.driver_running(self.push):
                raise Failure('loop helper refused or postcondition failed')
        except (OSError, Failure):
            self.restore(path, before)
            self.safe(self.push/'rq/stop').unlink(missing_ok=True)
            recovery = 0
            if not self.os.driver_running(self.push):
                recovery = self.os.helper(self.push)
                self.safe(self.push/'rq/stop').unlink(missing_ok=True)
                if not recovery and ((self.push/'rloop.run.sh').read_bytes() != path.read_bytes() or not self.os.driver_running(self.push)):
                    recovery = 1
            self.record['status'] = 'loop recovery failed' if recovery else 'loop refused; run again later'
            self.save()
            raise Failure(self.record['status']) from None
        self.record.pop('loop_attempt', None)
        self.save()

    def poller(self, installed_at, reuse=False):
        self.stage = 'poller confirmation'
        path = self.path('root/telegram/tg-poll.py')
        source, identity = source_identity(path)
        entry_identity = self.snapshot(path)
        old = self.os.pollers(path)
        if len(old) > 1:
            raise Failure('poller ownership ambiguous')
        if reuse and len(old) == 1 and old[0] == self.record.get('poller') and old[0]['source'] == source and old[0]['identity'] == identity:
            return
        old_ids = {(p['pid'], p['generation']) for p in old}
        for proc in old:
            self.os.stop(proc)
        deadline = self.os.now()+180
        exited = not old
        while self.os.now() < deadline:
            if self.snapshot(path) != entry_identity or source_identity(path) != (source, identity):
                raise Failure('poller link/source changed during confirmation')
            current = self.os.pollers(path)
            if len(current) > 1:
                raise Failure('poller ownership ambiguous')
            if not any((p['pid'], p['generation']) in old_ids for p in current):
                exited = True
            if current and exited:
                p = current[0]
                if p['started'] <= installed_at or p['source'] != source or p['identity'] != identity:
                    raise Failure('poller wrong startup source or stale generation')
                self.record['poller'] = p
                self.save()
                return
            self.os.sleep(1)
        raise Failure('poller stop/start timeout; stopgaps retained')

    def restore_stopgaps(self):
        current = self.os.cron_read().splitlines()
        originals = [s for s in self.record['cron'].splitlines() if active(s) and STOPGAP.search(s)]
        current = [s for s in current if not (active(s) and STOPGAP.search(s))]
        self.os.cron_write('\n'.join(current+originals)+'\n')
        self.path(MARKER).unlink(missing_ok=True)
        self.record['retiring'] = False
        self.save()

    def run_locked(self, revert):
        record_path = self.state/'record.json'
        if record_path.exists():
            self.load()
        elif revert:
            raise Failure('missing backup; cannot revert')
        if self.record and self.record.get('retiring'):
            self.restore_stopgaps()
        if revert:
            if self.record.get('status') == 'reverted':
                return 0
            self.loop_step(self.record['images']['push/rloop.sh'])
            self.stage = 'restore targets'
            installed_at = self.os.now()
            for key in TARGETS:
                if key not in ('push/rloop.sh', MARKER) and (self.record['images'][key]['kind'] != 'missing' or key in self.record.get('touched', [])):
                    self.restore(self.path(key), self.record['images'][key])
            self.os.cron_write(self.record['cron'])
            self.path(MARKER).unlink(missing_ok=True)
            self.poller(installed_at)
            self.record['status'] = 'reverted'
            self.save()
            return 0
        if self.record and self.record.get('status') == 'reverted':
            raise Failure('pre-image already reverted; archive it before another activation')
        patches = self.patches()
        cron = self.cron_install(self.os.cron_read())
        for key, source in LINKS.items():
            if not (self.repo/source).is_file():
                raise Failure('required versioned install source missing')
            self.path(key)
        self.safe(self.push/'restart-driver.sh')
        if (self.push/'restart-driver.sh').is_symlink() or (self.push/'rloop.run.sh').is_symlink():
            raise Failure('helper/execution copy must be regular files')
        if not (self.push/'restart-driver.sh').is_file():
            raise Failure('unchanged restart helper missing')
        if self.record is None:
            self.stage = 'pre-image'
            images = {key: self.snapshot(self.path(key)) for key in TARGETS}
            if images['push/rloop.sh']['kind'] != 'file':
                raise Failure('loop source must be regular file')
            self.record = dict(version=1, root=str(self.root), push=str(self.push), images=images,
                               cron=self.os.cron_read(), execution_copy=self.snapshot(self.path('push/rloop.run.sh')),
                               status='backed up', installed={}, touched=[])
            self.save()
        self.record['status'] = 'applying'
        self.save()
        self.loop_step(dict(kind='file', data=base64.b64encode(patches['push/rloop.sh'].encode()).decode(),
                            mode=self.path('push/rloop.sh').stat().st_mode & 0o777))
        self.stage = 'install targets'
        installed_at = self.os.now()
        for key, source in LINKS.items():
            if key not in self.record['touched']:
                self.record['touched'].append(key)
                self.save()
            desired = dict(kind='link', target=str(self.repo/source))
            if self.snapshot(self.path(key)) != desired:
                self.restore(self.path(key), desired)
        for key, text in patches.items():
            if key != 'push/rloop.sh':
                path = self.path(key)
                atomic(path, text.encode(), path.stat().st_mode & 0o777)
            self.record['installed'][key] = hashlib.sha256(text.encode()).hexdigest()
        self.save()
        for key in LEGACY:
            self.path(key).unlink(missing_ok=True)
        self.os.cron_write(cron)
        self.poller(installed_at, reuse=True)
        self.stage = 'sender gate'
        code = self.os.gate(self.repo)
        if code:
            # Thinker ruling: unconverted callers must retain their original transport.
            self.restore(self.path('root/telegram/tg-send.sh'), self.record['images']['root/telegram/tg-send.sh'])
            self.restore_stopgaps()
            raise Failure('ordinary tg-check failed; sender pre-image restored; stopgaps retained')
        self.stage = 'retirement'
        self.record['retiring'] = True
        self.save()
        try:
            before = self.os.cron_read().splitlines(keepends=True)
            removed = [s for s in before if active(s) and STOPGAP.search(s)]
            self.os.cron_write(''.join(s for s in before if not (active(s) and STOPGAP.search(s))))
            atomic(self.path(MARKER), b'gated\n')
            self.record['retiring'] = False
            self.record['status'] = 'applied'
            self.save()
            print('Retired '+str(len(removed))+' temporary cron invocations: ordinary sender gate passed.')
        except (OSError, Failure):
            self.restore_stopgaps()
            raise
        return 0

    def run(self, dry_run=False, revert=False):
        if dry_run and revert:
            raise Failure('dry-run and revert are mutually exclusive')
        try:
            if dry_run:
                if (self.state/'record.json').exists():
                    self.load()
                self.patches()
                self.cron_install(self.os.cron_read())
                for key, source in LINKS.items():
                    self.path(key)
                    if not (self.repo/source).is_file():
                        raise Failure('required versioned install source missing')
                for key in TARGETS:
                    print('Plan target: '+key)
                print('Plan: durable backup; atomic loop source; unchanged helper; install; poller; ordinary tg-check; retirement only on zero.')
                code = self.os.gate(self.repo)
                print('Current sender gate status: '+str(code)+'; dry-run makes no changes.')
                return 0
            self.stage = 'operation lock'
            self.safe(self.state/'lock')
            self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.state.is_symlink() or stat.S_IMODE(self.state.stat().st_mode) & 0o077:
                raise Failure('backup directory must be private')
            fd = os.open(self.state/'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Failure('another cutover operation is active') from None
                return self.run_locked(revert)
        except (OSError, ValueError, KeyError, TypeError, SyntaxError, Failure) as exc:
            reason = str(exc) if isinstance(exc, Failure) else type(exc).__name__
            print('telegram-cutover failed at '+self.stage+': '+reason+'; stopgaps retained; use --revert or run again later.', file=sys.stderr)
            return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit reversible Telegram activation; run --dry-run first.')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--dry-run', action='store_true')
    group.add_argument('--revert', action='store_true')
    args = parser.parse_args(argv)
    return Cutover(Path(os.environ.get('DOIT_ROOT', Path.home()/'.do-it')), Path('/var/tmp/push')).run(args.dry_run, args.revert)


if __name__ == '__main__':
    sys.exit(main())
