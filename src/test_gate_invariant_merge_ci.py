"""Exact-tree/scoped-CI admission regressions against the production driver.

Run standalone: /usr/bin/python3 src/test_gate_invariant_merge_ci.py AC1Tests
Only external commands are controlled; rloop's decision functions are unchanged.
"""
import atexit
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1]
LIVE_EVENTS = Path('/home/albert/.do-it/events')
FIXTURE_SOURCE = Path('/home/albert/.do-it/repos/albert-scott')
FIXTURE_PIN = '0a5f322'


def live_metadata():
    # Metadata supplies append offsets; never read pre-existing event contents.
    result = {}
    for path in LIVE_EVENTS.glob('*.jsonl'):
        stat = path.stat()
        result[str(path)] = (stat.st_size, stat.st_mtime_ns)
    return result


# Isolation must precede *all* imports which can transitively import fold.
LIVE_BEFORE_IMPORT = live_metadata()
OWNED = tempfile.TemporaryDirectory(prefix='gate-invariant-')
OWNED_ROOT = Path(OWNED.name)
OWNED_PIDS = set()
OWNED_PROCESS_IDENTITIES = set()
OWNED_IDS = {'L-spec-990001', 'L-spec-990002', 'L-spec-990003',
             'l-spec-990001', 'l-spec-990002', 'l-spec-990003'}
RH_WINDOW = f'rh-{os.getpid()}-1'
OWNED_IDS.add(RH_WINDOW)


def window_id(label):
    value = f'invariant-2267-{os.getpid()}-{label}'
    OWNED_IDS.add(value)
    return value


atexit.register(OWNED.cleanup)
for name in ('events', 'state', 'content', 'home', 'config'):
    (OWNED_ROOT / name).mkdir()
os.environ['DOIT_ROOT'] = str(OWNED_ROOT)
os.environ['DOIT_GATE_LEDGER_FILE'] = 'L-executor-invariant.jsonl'
os.environ['DOIT_LEDGER_FILE'] = 'L-executor-invariant.jsonl'
os.environ['DOIT_NO_POKE'] = '1'
os.environ['HOME'] = str(OWNED_ROOT / 'home')
os.environ['XDG_CONFIG_HOME'] = str(OWNED_ROOT / 'config')
(OWNED_ROOT / 'grading.toml').write_text('')
os.environ['DOIT_GRADING_TOML'] = str(OWNED_ROOT / 'grading.toml')

import panes  # noqa: E402


def track_process(pid):
    OWNED_PIDS.add(pid)
    start = panes.proc_start(pid)
    if start is not None:
        OWNED_PROCESS_IDENTITIES.add((pid, str(start)))


track_process(os.getpid())


class OwnedPopen(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        track_process(self.pid)


# Observe actual child creation, preserving subprocess execution and results.
subprocess.Popen = OwnedPopen

import cpu_share  # noqa: E402
import window_gate  # noqa: E402
import fold  # noqa: E402
import merge_gate  # noqa: E402
import merge_queue  # noqa: E402
import quarantine_file  # noqa: E402
import rechain  # noqa: E402


def report(name, body):
    encoded = json.dumps(body, sort_keys=True, separators=(',', ':')).encode()
    print(json.dumps(dict(url='cli://gate-invariant/' + name, status=200,
                          body_sha256=hashlib.sha256(encoded).hexdigest(),
                          body_excerpt=body)), flush=True)


def command(argv, *, cwd=None, env=None, timeout=60):
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode:
        # Commands here contain owned fixture paths, never endpoint values.
        detail = '\n'.join(rechain.stderr_tail(result.stdout + '\n' + result.stderr))
        detail = re.sub(r'postgres(?:ql)?://\S+', '[redacted endpoint]', detail)
        raise AssertionError(f'{Path(argv[0]).name} failed: exit={result.returncode}: {detail}')
    return result.stdout.strip()


def git(repo, *args):
    return command(['/usr/bin/git', '-C', str(repo), '-c', 'user.name=invariant',
                    '-c', 'user.email=invariant@localhost', *args])


class DriverFixture:
    """Owned refs and CLI responses for one bounded production loop invocation."""

    def __init__(self, root, *, complete=False):
        self.root = root
        self.repo, self.mirror = root / 'repo', root / 'mirror'
        self.q, self.bin, self.d = root / 'q', root / 'bin', root / 'doit'
        for path in (self.q, self.bin, self.d):
            path.mkdir(parents=True)
        (self.d / 'src').symlink_to(SOURCE / 'src', target_is_directory=True)
        self.calls, self.gitcalls = root / 'calls', root / 'gitcalls'
        self.ready = root / 'ready'
        self.ready.write_text('')
        pin = git(FIXTURE_SOURCE, 'rev-parse', '--verify', FIXTURE_PIN + '^{commit}')
        if complete:
            command(cpu_share.niced(['git', 'clone', '-q', '--shared', '--no-checkout',
                                    str(FIXTURE_SOURCE), str(self.repo)]), timeout=300)
            git(self.repo, 'checkout', '-q', '-B', 'master', pin)
            git(self.repo, 'remote', 'remove', 'origin')
            self.base = pin
        else:
            git(root, 'init', '-q', '-b', 'master', str(self.repo))
            for path in ('scripts/ci/predeploy_gate.sh', 'scripts/ci/actions_changes_gate.sh'):
                self.write(path, git(FIXTURE_SOURCE, 'show', pin + ':' + path) + '\n')
            self.write('dashboard/src/invariant.ts', 'export const invariant = 0;\n')
            self.base = self.commit('fixture base')
        git(self.repo, 'checkout', '-q', '-b', 'l-spec-990001')
        self.write('dashboard/src/invariant.ts', 'export const invariant = 1;\n')
        self.candidate = self.commit('fixture candidate')
        git(self.repo, 'checkout', '-q', 'master')
        git(root, 'init', '-q', str(self.mirror))
        if not complete:
            git(self.mirror, 'fetch', '-q', str(self.repo),
                '+refs/heads/*:refs/integ/*')
        (self.q / 'pushed').write_text(self.base + '\n')
        self.env = dict(PATH=str(self.bin) + ':/usr/bin:/bin',
                        HOME=str(OWNED_ROOT / 'home'), DOIT_ROOT=str(OWNED_ROOT),
                        DOIT_GATE_LEDGER_FILE='L-executor-invariant.jsonl',
                        DOIT_LEDGER_FILE='L-executor-invariant.jsonl', DOIT_NO_POKE='1',
                        RLOOP_Q=str(self.q), RLOOP_READY=str(self.ready),
                        RLOOP_R=str(self.mirror), RLOOP_IR=str(self.repo),
                        RLOOP_D=str(self.d), RLOOP_PROJECT='albert-scott',
                        RLOOP_TG_SEND=str(self.bin / 'tgsend'),
                        RLOOP_UNTIL='99999999999', CALLS=str(self.calls),
                        GITCALLS=str(self.gitcalls), OWNED_FIXTURE=str(root),
                        BASE_REPLY=str(root / 'base-reply.json'))
        self.executable(self.d / 'doit', '''
printf '%s\\n' "$*" >> "$CALLS"
case "$1" in
  base-red) cat "$BASE_REPLY";;
  gate-window) exit 0;;
  *) exit 97;;
esac
''')
        self.executable(self.bin / 'pgrep', 'exit 1\n')
        self.executable(self.bin / 'sleep', 'exit 0\n')
        self.executable(self.bin / 'tgsend', 'exit 0\n')
        # Fail closed at hosted service boundaries even if the driver changes.
        for name in ('gh', 'curl', 'wget', 'ssh'):
            self.executable(self.bin / name,
                            'printf "forbidden-service %s\\n" "$*" >> "$CALLS"; exit 98\n')
        self.executable(self.bin / 'git', '''
printf '%s\\n' "$*" >> "$GITCALLS"
[ "$1" = -C ] || exit 96
case "$2" in "$OWNED_FIXTURE"/*) ;; *) exit 96;; esac
case " $* " in *" push "*|*" merge "*|*" update-ref "*) exit 95;; esac
exec /usr/bin/git "$@"
''')
        self.base_red([])
        self.required = window_gate.required_checks(self.repo, self.base)
        self.scoped = window_gate.scope(self.repo, self.base, self.candidate,
                                         self.required)['checks']
        self.tree = window_gate.tree_of(self.repo, self.candidate)
        report('fixture', dict(pin=pin, base=self.base, candidate=self.candidate,
                               tree=self.tree, required=self.required, scoped=self.scoped))

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message):
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', message)
        return git(self.repo, 'rev-parse', 'HEAD')

    @staticmethod
    def executable(path, body):
        path.write_text('#!/bin/bash\nset -u\n' + body)
        path.chmod(0o700)

    def base_red(self, ids):
        (self.root / 'base-reply.json').write_text(json.dumps(dict(
            sha=self.base, complete=True, ids={'pytest': ids}, undetermined=[])))

    def batch(self, ci):
        path = self.q / '1.batch'
        path.write_text(self.base + ' 990001\n')
        Path(str(path) + '.cand').write_text(self.candidate + '\n')
        if ci is not None:
            Path(str(path) + '.ci').write_text(ci)
        # Suppress stacking another batch during the queued/in-progress case.
        (self.q / ('nostack-' + self.candidate[:12])).touch()

    def run(self, shell):
        source = shlex.quote(str(SOURCE / 'lane/rloop.sh'))
        script = f'source {source}; rloop_init; {shell}; status=$?; wait; exit "$status"'
        return command(cpu_share.niced(['bash', '-c', script]),
                       cwd=self.root, env=self.env)

    def lines(self, path):
        return path.read_text().splitlines() if path.exists() else []

    def log(self):
        return '\n'.join(self.lines(self.q / 'rloop.log'))

    def assert_safe(self, test):
        test.assertEqual(git(self.repo, 'rev-parse', 'master'), self.base)
        for line in self.lines(self.gitcalls):
            test.assertFalse(any(' ' + verb + ' ' in ' ' + line + ' '
                                 for verb in ('merge', 'update-ref', 'push')), line)
        test.assertFalse(any(line.startswith('forbidden-service ')
                             for line in self.lines(self.calls)))


def mentioned_processes(line):
    """Pair each PID with its own recorded start ticks, never a sibling's."""
    found = set()

    def visit(value):
        if isinstance(value, dict):
            for key, pid in value.items():
                if key == 'pid':
                    starts = ('proc_start', 'procStart', 'start_ticks')
                elif key.endswith('_pid'):
                    starts = (key[:-3] + 'proc_start',)
                else:
                    continue
                if isinstance(pid, (str, int)) and str(pid).isdigit():
                    for name in starts:
                        start = value.get(name)
                        if isinstance(start, (str, int)) and str(start).isdigit():
                            found.add((int(pid), str(start)))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    try:
        visit(json.loads(line))
    except ValueError:
        pass  # Owned paths and ids still identify partial append records.
    return found


class AC1Tests(unittest.TestCase):
    def setUp(self):
        self.start = time.monotonic()
        self.usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        self.tmp = tempfile.TemporaryDirectory(prefix='driver-', dir=OWNED_ROOT)
        # LIFO: collect owned identities, remove fixtures, then check live appends.
        self.addCleanup(self.check_live)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.collect_owned_ids)
        self.root = Path(self.tmp.name)

    def collect_owned_ids(self):
        # Read only completed owned fixture stores before their directories vanish.
        for path in OWNED_ROOT.rglob('events/*.jsonl'):
            for line in path.read_text().splitlines():
                event = json.loads(line)
                for key in ('subject', 'window', 'spec', 'branch'):
                    value = event.get(key)
                    if isinstance(value, str) and value:
                        OWNED_IDS.add(value)

    def check_live(self):
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        before, after = LIVE_BEFORE_IMPORT, live_metadata()
        changed = sorted(path for path in before.keys() | after.keys()
                         if before.get(path) != after.get(path))
        # The live ledger is continuously written. Inspect only bytes appended
        # since the pre-import snapshot, never historical contents or raw output.
        tokens = [str(OWNED_ROOT), *sorted(OWNED_IDS)]
        owned = re.compile('|'.join(re.escape(token) for token in tokens))
        matches, appended_lines = 0, 0
        findings = []
        unreadable = []
        for name in sorted(before.keys() | after.keys()):
            offset = before.get(name, (0, 0))[0]
            if name not in after or after[name][0] < offset:
                unreadable.append(name)
                continue
            remaining = after[name][0] - offset
            with open(name, 'rb') as stream:
                stream.seek(offset)
                while remaining:
                    line = stream.readline(remaining)
                    if not line:
                        unreadable.append(name)
                        break
                    remaining -= len(line)
                    appended_lines += 1
                    decoded = line.decode('utf-8', errors='replace')
                    token = owned.search(decoded)
                    identities = mentioned_processes(decoded) & OWNED_PROCESS_IDENTITIES
                    pids = {pid for pid, _ in identities}
                    if token or pids:
                        matches += 1
                        findings.append(dict(file=Path(name).name,
                            owned_identifier=token.group() if token else None,
                            owned_pids=sorted(pids),
                            owned_process_identities=sorted(identities),
                            line_sha256=hashlib.sha256(line).hexdigest()))
        report(self.__class__.__name__.removesuffix('Tests') + '-isolation',
            dict(metadata_unchanged=before == after, files=len(after),
            changed_metadata={name: dict(before=before.get(name), after=after.get(name))
                              for name in changed},
            appended_lines_checked=appended_lines, owned_matches=matches,
            tracked_processes=len(OWNED_PIDS), unreadable_files=len(unreadable),
            tracked_process_identities=len(OWNED_PROCESS_IDENTITIES),
            identifier_findings=findings,
            wall_s=round(time.monotonic() - self.start, 3),
            cpu_s=round(usage.ru_utime + usage.ru_stime -
                        self.usage.ru_utime - self.usage.ru_stime, 3)))
        self.assertFalse(unreadable, 'live append range unavailable; invariance unproven')
        self.assertEqual(matches, 0, 'owned fixture identifier found in live ledger append')

    def test_launch_matrix(self):
        cases = ('scoped-green', 'red', 'undetermined', 'queued', 'in-progress',
                 'missing-checks', 'short-green', 'different-tree', 'preflight-only',
                 'different-base', 'different-candidate', 'last-exit-red',
                 'last-verdict-red')
        results = []
        for name in cases:
            with self.subTest(case=name):
                fixture = DriverFixture(self.root / name)
                self.assertGreaterEqual(len(fixture.scoped), 2)
                self.assertLess(len(fixture.scoped), len(fixture.required))
                checked_tree = fixture.tree
                checks = fixture.scoped
                state, exit_code = 'green', 0
                if name in ('red', 'undetermined'):
                    state, exit_code = name, 1 if name == 'red' else 2
                elif name in ('queued', 'in-progress', 'preflight-only'):
                    state = name
                elif name in ('missing-checks', 'short-green'):
                    checks = [] if name == 'missing-checks' else fixture.scoped[:-1]
                elif name == 'different-tree':
                    checked_tree = window_gate.tree_of(fixture.repo, fixture.base)
                    self.assertNotEqual(checked_tree, fixture.tree)
                prior = dict(green=state == 'green', required=checks, tree=checked_tree)
                if name not in ('queued', 'in-progress', 'preflight-only'):
                    window_gate.ev('window-tree-verified', RH_WINDOW, window=RH_WINDOW,
                                   sha=fixture.candidate, **prior)
                if name in ('queued', 'in-progress'):
                    # A running rehearsal has no REHEARSED verdict yet. Otherwise
                    # derive_exit correctly recovers an orphaned poller's verdict.
                    ci = f'CI {name} window={RH_WINDOW}\n'
                else:
                    # Incomplete hosted checks cannot produce rehearsal green.
                    if name in ('missing-checks', 'short-green'):
                        state, exit_code = 'undetermined', 2
                    ci = (f'REHEARSED {RH_WINDOW} {state} base={fixture.base} '
                          f'candidate={fixture.candidate} tree={checked_tree}\n')
                    ci += f'exit={exit_code}\n'
                    if name == 'different-base':
                        ci = ci.replace('base=' + fixture.base, 'base=' + fixture.candidate)
                    elif name == 'different-candidate':
                        ci = ci.replace('candidate=' + fixture.candidate, 'candidate=' + fixture.base)
                    elif name == 'last-exit-red':
                        ci += 'exit=2\n'
                    elif name == 'last-verdict-red':
                        ci += f'REHEARSED {RH_WINDOW} red\n'

                fixture.batch(None if name == 'preflight-only' else ci)
                fixture.run('loop_once')
                fixture.assert_safe(self)
                launches = [line for line in fixture.lines(fixture.calls)
                            if line.startswith('gate-window ')]
                eligible = name == 'scoped-green'
                row = dict(case=name, expected_launch=eligible, launches=launches,
                           candidate_tree=fixture.tree, checked_tree=checked_tree,
                           required=fixture.required, scoped=fixture.scoped,
                           covered=checks, covers=window_gate.covers(prior, fixture.scoped),
                           trace=fixture.log())
                results.append(row)
                # Report before asserting so every refusal/invalid launch survives failure.
                report('AC1-' + name, row)
                self.assertEqual(bool(launches), eligible,
                                 f'{name}: driver admitted invalid CI evidence')
                if eligible:
                    self.assertEqual(len(launches), 1)
                    self.assertIn('--project albert-scott --specs L-spec-990001', launches[0])
        report('AC1-matrix', results)
        self.assertEqual(len(results), len(cases))

    def test_live_ledger_process_attribution(self):
        pid = os.getpid()
        start = panes.proc_start(pid)
        self.assertIsNotNone(start)
        events = self.root / 'live-events'
        events.mkdir()
        ledger = events / 'L-reviewer-fixture.jsonl'
        cases = (
            ('pid-only receipt', dict(type='reviewed', pid=pid), False),
            ('reused pid', dict(pid=pid, proc_start=str(int(start) + 1)), False),
            ('unrelated nested identity', dict(pid=pid,
                process=dict(pid=pid + 1, proc_start=start)), False),
            ('matching identity', dict(pid=pid, proc_start=start), True),
            ('matching waiter identity', dict(waiter_pid=pid, waiter_proc_start=start), True),
            ('matching harness identity', dict(pid=pid, procStart=start), True),
            ('matching integer ticks', dict(pid=pid, start_ticks=int(start)), True),
            ('owned id and pid', dict(pid=pid, window=RH_WINDOW), True),
            ('owned path', dict(path=str(self.root / 'fixture')), True),
        )
        for label, event, attributable in cases:
            with self.subTest(case=label):
                ledger.write_bytes(b'')
                stat = ledger.stat()
                before = {str(ledger): (stat.st_size, stat.st_mtime_ns)}
                with ledger.open('a') as stream:
                    stream.write(json.dumps(event) + '\n')
                with patch.dict(globals(), LIVE_EVENTS=events, LIVE_BEFORE_IMPORT=before), \
                        patch(__name__ + '.report'):
                    if attributable:
                        with self.assertRaisesRegex(AssertionError, 'owned fixture identifier'):
                            self.check_live()
                    else:
                        self.check_live()
        report('AC1-ledger-attribution', dict(cases=len(cases),
            pid_only_receipt='allowed', reused_pid='allowed',
            owned_path_id_or_matching_process_identity='rejected'))

    def test_base_red_hold_and_both_release_keys(self):
        results = []
        for release in ('member', 'failing-ids'):
            with self.subTest(release=release):
                fixture = DriverFixture(self.root / release)
                node = 'tests/invariant.py::test_existing_red'
                fixture.base_red([node])
                fixture.batch(f'REHEARSED {RH_WINDOW} red\nUNRESOLVED {node}\nexit=1\n')
                fixture.run('loop_once')
                self.assertIn('base-red-hold 1.batch ' + node, fixture.log())
                for _ in range(2):
                    held = fixture.run('base_red_held 990001; printf "%s" "$?"')
                    self.assertEqual(held, '0')
                    fixture.run('loop_once')
                self.assertEqual(fixture.lines(fixture.calls),
                                 [line for line in fixture.lines(fixture.calls)
                                  if line.startswith('base-red ')])
                if release == 'member':
                    git(fixture.repo, 'checkout', '-q', 'l-spec-990001')
                    fixture.write('dashboard/src/invariant.ts', 'export const invariant = 2;\n')
                    fixture.commit('member changed')
                    git(fixture.repo, 'checkout', '-q', 'master')
                    fixture.run('sync')
                else:
                    fixture.base_red([])
                self.assertEqual(fixture.run('base_red_held 990001; printf "%s" "$?"'), '1')
                self.assertIn('base-red-release 1.batch', fixture.log())
                fixture.assert_safe(self)
                results.append(dict(release=release, trace=fixture.log()))
        report('AC1-base-red-hold', results)


class WindowFixture(DriverFixture):
    """Reuse the owned Git fixture with production queue, admission and landing."""

    def __init__(self, root, test, *, complete=False):
        super().__init__(root, complete=complete)
        self.test = test
        self.migration_fixture = complete
        self.origin = root / 'origin.git'
        if complete:
            git(root, 'clone', '-q', '--shared', '--bare', str(self.repo), str(self.origin))
        else:
            git(root, 'init', '-q', '--bare', str(self.origin))
        git(self.repo, 'remote', 'add', 'origin', str(self.origin))
        git(self.repo, 'push', '-q', 'origin', 'master')
        repos = root / 'repos'
        repos.mkdir()
        (repos / 'albert-scott').symlink_to(self.repo, target_is_directory=True)
        for name in ('state', 'content', 'events'):
            (root / name).mkdir()
        (root / 'look.toml').write_text('[merge_queue]\nbatch_cap = 15\n')
        self.spec = 'L-spec-990001'
        self.branch = 'l-spec-990001'
        self.extra_members = []
        self.timeline = []
        self.stack = ExitStack()
        test.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(fold, 'ROOT', root))
        self.stack.enter_context(patch.object(fold, 'EVENTS', root / 'events'))
        # Queue configuration is a fixture file; no selection/CI decisions patched.
        self.stack.enter_context(patch.object(merge_queue, 'DOIT', str(root / 'doit')))
        self.stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(root),
            DOIT_REHEARSAL_LAUNCHER='off', DOIT_MAIN='master',
            DOIT_WINDOW_MAX_MERGES='15'))
        self.grant = ['dashboard/src/invariant.ts']
        self.metadata()

    def metadata(self):
        (self.root / 'content' / (self.spec + '.md')).write_text(
            'Writes: ' + ', '.join(self.grant) + '\n')
        window_gate.ev('spec-written', self.spec, branch=self.branch,
                       file=str(self.root / 'content' / (self.spec + '.md')))

    def events(self):
        # Independent completed reader, with isolation inherited before imports.
        output = command(['/usr/bin/python3', '-c',
            'import sys; sys.path.insert(0, sys.argv[1]); import fold,json; '
            'print(json.dumps(fold.read_events(use_cache=False)))', str(SOURCE / 'src')],
            env=rechain.child_env())
        return json.loads(output)

    def classify(self, spec=None, branch=None):
        spec, branch = spec or self.spec, branch or self.branch
        before = git(self.repo, 'rev-parse', 'master')
        output = command(['/usr/bin/python3', '-c',
            'import sys; sys.path.insert(0, sys.argv[1]); import merge_gate; '
            'sys.exit(merge_gate.main_(sys.argv[2:]))', str(SOURCE / 'src'),
            branch, 'master', '--spec', spec],
            cwd=self.repo, env=rechain.child_env())
        self.test.assertEqual(git(self.repo, 'rev-parse', 'master'), before)
        clean = [e for e in self.events() if e['type'] == 'merge-gate-clean'
                 and e['subject'] == spec]
        self.test.assertEqual(len(clean), 1, output)
        self.timeline.append(dict(step='dry-run-clean', tree=clean[0]['merged_tree']))
        self.test.assertFalse(any(e['type'] == 'shipped' for e in self.events()))
        window_gate.ev('queue-add', spec, branch=branch, rule='operator')
        return clean[0]

    def seed(self, tree, checks, *, stale=False):
        original = fold.NOW
        # Use the writer's clock for a real persisted, expired cache record.
        if stale:
            original_ev = datetime.now(timezone.utc) - timedelta(days=3)
            tokens = ['window-tree-verified', window_id('prior'), 'green:=true',
                      'window=' + window_id('prior'), 'sha=' + self.candidate,
                      'tree=' + tree, 'required:=' + json.dumps(checks),
                      'project=albert-scott']
            try:
                fold.NOW = original_ev
                fold.append(tokens)
            finally:
                fold.NOW = original
        else:
            window_gate.ev('window-tree-verified', window_id('prior'), tree=tree,
                           green=True, required=checks, window=window_id('prior'), sha=self.candidate)

    def run_window(self, state='green', *, queue=False, cache=None, race=False,
                   fingerprint_cache=None, remainder=False):
        before_events = len(self.events())
        members = [(self.spec, self.branch, self.candidate), *self.extra_members]
        spec_args = [s for s, _, _ in members]
        candidate, kept, clashes = window_gate.build_candidate(
            self.repo, window_id('preview'), self.base, members, publish=False)
        self.test.assertEqual(kept, spec_args)
        self.test.assertEqual(clashes, {})
        if self.migration_fixture:
            # Inspect assembly without substituting its fingerprint result.
            self.test.assertNotEqual(candidate, self.base)
        tree = window_gate.tree_of(self.repo, candidate)
        checks = window_gate.scope(self.repo, self.base, candidate, self.required)['checks']
        calls = []
        launched_specs = [self.spec]
        real_fingerprint = window_gate.fingerprint_candidate
        real_policy = quarantine_file.candidate

        def fingerprint(*args, **kwargs):
            final = real_fingerprint(*args, **kwargs)
            final_tree = window_gate.tree_of(self.repo, final)
            self.timeline.append(dict(step='fingerprint', candidate=final, tree=final_tree))
            return final

        def policy(*args, **kwargs):
            final = real_policy(*args, **kwargs)
            final_tree = window_gate.tree_of(self.repo, final)
            if cache:
                wanted = window_gate.scope(self.repo, self.base, final, self.required)['checks']
                self.seed(window_gate.tree_of(self.repo, self.base) if cache == 'other-tree'
                          else final_tree, [] if cache == 'missing' else
                          wanted[:-1] if cache == 'short' else wanted, stale=cache == 'stale')
            if fingerprint_cache:
                wanted = window_gate.scope(self.repo, self.base, final, self.required)['checks']
                self.seed(final_tree, wanted[:-1])
            return final

        def hosted(repo, sha, required, window, **kwargs):
            if sha == self.base:
                self.test.assertEqual(required, [window_gate.PG_CHECK])
                self.timeline.append(dict(step='base-verify', candidate=sha, checks=required))
                return dict(state='green', nodes=[], url='', doc={})
            actual_tree = window_gate.tree_of(repo, sha)
            expected = window_gate.scope(repo, self.base, sha, self.required)['checks']
            self.test.assertEqual(required, expected)
            bad_present = remainder and rechain.git(repo, 'cat-file', '-e',
                sha + ':dashboard/src/remainder.test.ts', check=False).returncode == 0
            verdict = 'red' if bad_present else state
            calls.append(dict(candidate=sha, tree=actual_tree, checks=required, state=verdict))
            self.timeline.append(dict(step='verify', **calls[-1]))
            if race:
                sibling = getattr(self, 'sibling', None) or git(
                    repo, 'commit-tree', window_gate.tree_of(repo, self.base),
                    '-p', self.base, '-m', 'concurrent sibling')
                git(repo, 'update-ref', 'refs/heads/master', sibling, self.base)
                self.timeline.append(dict(step='sibling', sha=sibling))
            return dict(state=verdict,
                nodes=['dashboard/src/remainder.test.ts::test_red'] if bad_present else [],
                url='', doc={}, why='controlled hosted response')

        real_advance = window_gate.advance

        def landing(*args, **kwargs):
            self.timeline.append(dict(step='advance', old=args[2], candidate=args[3],
                                      tree=window_gate.tree_of(args[0], args[3])))
            return real_advance(*args, **kwargs)

        def runner(argv):
            self.test.assertEqual(argv[:2], ['doit', 'gate-window'])
            self.test.assertEqual(argv[3:6], ['--project', 'albert-scott', '--specs'])
            launched_specs[:] = argv[6].split(',')
            self.test.assertIn(self.spec, launched_specs)
            automatic = [spec for spec in launched_specs if spec != self.spec]
            for spec in automatic:
                self.test.assertTrue(spec.startswith('quarantine-'))
                clean = [e for e in self.events() if e['type'] == 'merge-gate-clean'
                         and e['subject'] == spec]
                self.test.assertTrue(clean)
                self.test.assertEqual(clean[-1]['grant'], [quarantine_file.PATH])
            self.timeline.append(dict(step='queue-launch', argv=argv,
                automatic_count=len(automatic), automatic_specs=automatic,
                reason='production queue reconciles expired pinned quarantine policy'))
            return dict(code=window_gate.main_(argv[2:]))

        with patch.object(window_gate, 'verify', side_effect=hosted), \
             patch.object(window_gate, 'fingerprint_candidate', side_effect=fingerprint), \
             patch.object(quarantine_file, 'candidate', side_effect=policy), \
             patch.object(window_gate, 'advance', side_effect=landing), \
             patch.object(window_gate, 'base_red_snapshot',
                          return_value=dict(sha=self.base, complete=True, ids={}, undetermined=[])):
            if queue:
                # A precheck is local guard evidence, not CI admission authority.
                result = merge_queue.window_run('albert-scott', runner=runner,
                    precheck_runner=lambda project: dict(
                        ts=datetime.now(timezone.utc).isoformat(), master=self.base,
                        clean=[self.spec], guard_red={}, conflict_master={}))
            else:
                result = window_gate.main_([window_id('direct'), '--project', 'albert-scott',
                                           '--specs', ','.join(spec_args)])
        # Read the committed refs in another process and the completed ledger.
        local = git(self.repo, 'rev-parse', 'refs/heads/master')
        remote = git(self.origin, 'rev-parse', 'refs/heads/master')
        events = self.events()[before_events:]
        shipped = [e for e in events if e['type'] == 'shipped']
        verified = [e for e in events if e['type'] == 'window-verified']
        self.test.assertEqual(remote, self.base)  # advance is local; push is a later producer.
        if result in (0, 1) and local != self.base:
            self.test.assertEqual({e['subject'] for e in shipped}, set(launched_specs))
            self.test.assertEqual(len(verified), 1)
            self.test.assertEqual(shipped[0]['sha'], local)
            self.test.assertEqual(verified[0]['sha'], local)
            self.test.assertEqual(verified[0]['scope'],
                                  window_gate.scope(self.repo, self.base, local, self.required)['checks'])
        else:
            self.test.assertEqual(shipped, [])
            self.test.assertEqual(verified, [])
        report('AC2-window', dict(state=state, queue=queue, cache=cache,
            result=result, local=local, remote=remote, base=self.base,
            required=self.required, scoped=checks, timeline=self.timeline,
            shipped=shipped, verified=verified))
        return result, local, calls


class RebuildFixture(WindowFixture):
    """Complete pinned source, additive branches, and observed real rebuilds."""

    overrides = ('DOIT_RECHAIN_FINGERPRINT_CMD', 'DOIT_RECHAIN_PG_CMD')
    python = '/opt/albert-scott/.venv/bin/python'
    alembic = '/opt/albert-scott/.venv/bin/alembic'
    object_name = 'doit_invariant_2267_object'
    recorded = None
    real_fingerprint = staticmethod(rechain.fingerprint)
    real_blob = staticmethod(rechain.blob)
    object_bytes = {}
    object_trees = {}

    def __init__(self, root, test, *, moved=False):
        for name in self.overrides:
            test.assertNotIn(name, os.environ, name + ' must be absent')
        for path in (self.python, self.alembic, '/usr/lib/postgresql/17/bin/pg_ctl'):
            test.assertTrue(os.access(path, os.X_OK), 'missing executable ' + path)
        super().__init__(root, test, complete=True)
        self.pin = self.base
        self.executions, self.clusters = [], []
        config = root / 'grading.toml'
        config.write_text('[project.albert-scott]\nvenv = "/opt/albert-scott/.venv"\n')
        self.stack.enter_context(patch.dict(os.environ, DOIT_GRADING_TOML=str(config),
                                            AS_ALEMBIC_BIN=self.alembic))
        for path in (rechain.MANIFEST, rechain.TEST, rechain.FINGERPRINT,
                     'scripts/refresh_genesis_fingerprint.sh', 'scripts/schema_snapshot_query.sql',
                     'api/scripts/resolve_alembic_deploy_target.py',
                     'scripts/ci/cleanup_listing_authority_scratch.py'):
            test.assertTrue((self.repo / path).is_file(), 'missing asset ' + path)
        self.stack.enter_context(patch.object(rechain, 'blob', side_effect=self.read_blob))
        self.parent = rechain.routed_head(self.repo, self.pin)
        self.a = self.migration('l-spec-990002', 'invariant_2267_a', measurable=False)
        self.b = self.migration(self.branch, 'invariant_2267_b', measurable=True)
        # The production dry-run gate finds its real scanner through the
        # project's venv. Keep the complete clone's tracked tree unchanged.
        test.assertTrue(os.access('/opt/albert-scott/.venv/bin/detect-secrets', os.X_OK))
        (self.repo / '.venv').symlink_to('/opt/albert-scott/.venv', target_is_directory=True)
        self.candidate = self.b
        self.sibling = self.a
        self.base = self.pin if moved else self.a
        git(self.repo, 'checkout', '-q', '--detach', self.pin)
        git(self.repo, 'update-ref', 'refs/heads/master', self.base, self.pin)
        git(self.repo, 'checkout', '-q', 'master')
        git(self.repo, 'push', '-q', 'origin', 'master')
        self.required = window_gate.required_checks(self.repo, self.base)
        self.grant = [rechain.VERSIONS + '*.py', rechain.MANIFEST, rechain.TEST,
                      rechain.FINGERPRINT, rechain.AUTHORITY]
        self.metadata()
        self.scoped = window_gate.scope(self.repo, self.base, self.b, self.required)['checks']
        self.stack.enter_context(patch.object(subprocess, 'run', side_effect=self.observe))
        self.stack.enter_context(patch.object(rechain, 'fingerprint',
                                             side_effect=self.recorded_fingerprint))
        report('rebuild-fixture', dict(pin=self.pin, base=self.base, a=self.a, b=self.b,
            required=self.required, scoped=self.scoped, python=self.python, alembic=self.alembic,
            overrides_absent=True))

    def read_blob(self, repo, rev, path):
        """Batch immutable Git reads; retain the production graph/resolver logic."""
        if not re.fullmatch(r'[0-9a-f]{40}', rev):
            return self.real_blob(repo, rev, path)
        key = (str(repo), rev)
        if key not in self.object_trees:
            result = self.real_run(['/usr/bin/git', '-C', str(repo), 'ls-tree', '-rz', rev],
                                   capture_output=True, check=True)
            entries = {}
            for entry in result.stdout.split(b'\0'):
                if not entry:
                    continue
                header, name = entry.split(b'\t', 1)
                _, kind, oid = header.split()
                name = name.decode()
                if kind == b'blob' and (name.startswith(rechain.VERSIONS)
                        or name in (rechain.MANIFEST, rechain.TEST,
                                    rechain.FINGERPRINT, rechain.AUTHORITY)):
                    entries[name] = oid.decode()
            missing = sorted(set(entries.values()) - self.object_bytes.keys())
            if missing:
                result = self.real_run(['/usr/bin/git', '-C', str(repo), 'cat-file', '--batch'],
                    input=('\n'.join(missing) + '\n').encode(), capture_output=True, check=True)
                offset = 0
                for wanted in missing:
                    end = result.stdout.index(b'\n', offset)
                    oid, kind, size = result.stdout[offset:end].split()
                    self.test.assertEqual((oid.decode(), kind), (wanted, b'blob'))
                    offset = end + 1
                    data = result.stdout[offset:offset + int(size)]
                    actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
                    self.test.assertEqual(actual, wanted)
                    self.object_bytes[wanted] = data
                    offset += int(size) + 1
                self.test.assertEqual(offset, len(result.stdout))
            self.object_trees[key] = entries
        oid = self.object_trees[key].get(path)
        return self.object_bytes[oid] if oid else self.real_blob(repo, rev, path)

    def schema_inputs(self, candidate):
        # The only schema-equivalent alternative is T0 without the additive
        # no-op sibling. Check its actual migration, rather than trusting a name.
        self.test.assertIn('def upgrade():\n    pass\n', rechain.text(
            self.repo, self.a, rechain.VERSIONS + 'invariant_2267_a.py'))
        # Queue policy changes affect CI scope/tree identity, checked by the
        # real judge below, but cannot affect the rebuilt database schema.
        ignored = {rechain.FINGERPRINT, rechain.MANIFEST, rechain.TEST, rechain.AUTHORITY,
                   quarantine_file.PATH,
                   rechain.VERSIONS + 'invariant_2267_a.py',
                   rechain.VERSIONS + 'invariant_2267_b.py'}
        entries = git(self.repo, 'ls-tree', '-r', candidate).splitlines()
        inputs = [line for line in entries if line.split('\t', 1)[1] not in ignored]
        migration = rechain.text(self.repo, candidate, rechain.VERSIONS + 'invariant_2267_b.py')
        revision, predecessor = rechain.revision(migration)
        self.test.assertEqual(revision, 'invariant_2267_b')
        self.test.assertIn(predecessor, (self.parent, 'invariant_2267_a'))
        inputs.append(migration.replace(repr(predecessor), repr(self.parent), 1))
        return hashlib.sha256('\n'.join(inputs).encode()).hexdigest()

    def recorded_fingerprint(self, repo, candidate, window, *, ev, project=None):
        """One measured real rebuild pair; replay its bytes for CI state cases.

        The amendment permits recorded fingerprints for the matrix. Assembly,
        scope, cache acceptance, verification and landing still run in production.
        Never reuse bytes for changed schema inputs.
        """
        self.test.assertEqual(Path(repo).resolve(), self.repo.resolve())
        if RebuildFixture.recorded is None:
            revision = rechain.revision(rechain.text(
                repo, candidate, rechain.VERSIONS + 'invariant_2267_b.py'))
            if revision[1] == 'invariant_2267_a':
                merged = candidate
            else:
                merged, reason = rechain.resolve(self.repo, self.a,
                    (self.spec, self.branch, self.b), window_id('shared-rebuild'), ev=window_gate.ev)
                self.test.assertIsNone(reason)
            self.assert_graph(merged)
            final = self.real_fingerprint(repo, merged, window, ev=window_gate.ev, project=project)
            self.assert_graph(final)
            self.independent_rebuild(final)
            RebuildFixture.recorded = dict(inputs=self.schema_inputs(final),
                content=rechain.blob(repo, final, rechain.FINGERPRINT))
        record = RebuildFixture.recorded
        self.test.assertEqual(self.schema_inputs(candidate), record['inputs'],
                             'recorded rebuild does not cover these schema inputs')
        tree = rechain.rewrite_tree(repo, window_gate.tree_of(repo, candidate),
                                   {rechain.FINGERPRINT: record['content']})
        final = git(repo, 'commit-tree', tree, '-p', candidate,
                    '-m', 'fixture: replay independently rebuilt fingerprint')
        ev('rechain-fingerprint', window, sha=final, recorded=True,
           fingerprint_sha256=hashlib.sha256(record['content']).hexdigest())
        report('recorded-fingerprint', dict(candidate=candidate, final=final,
            schema_inputs=record['inputs'],
            fingerprint_sha256=hashlib.sha256(record['content']).hexdigest()))
        return final

    def migration(self, branch, revision, *, measurable):
        git(self.repo, 'checkout', '-q', '-B', branch, self.pin)
        self.write(rechain.VERSIONS + revision + '.py',
            'from alembic import op\n' + f'revision = {revision!r}\n'
            + f'down_revision = {self.parent!r}\nbranch_labels = None\ndepends_on = None\n'
            + 'def upgrade():\n    ' + (f'op.execute("CREATE TABLE public.{self.object_name} '
                '(id integer PRIMARY KEY)")' if measurable else 'pass') + '\n'
            + 'def downgrade():\n    ' + (f'op.execute("DROP TABLE public.{self.object_name}")'
                if measurable else 'pass') + '\n')
        self.write(rechain.MANIFEST, rechain.manifest_edit(
            rechain.text(self.repo, self.pin, rechain.MANIFEST), self.parent, revision,
            ' Fixture additive successor ' + revision + '.'))
        self.write(rechain.TEST, rechain.test_edit(
            rechain.text(self.repo, self.pin, rechain.TEST), self.parent, revision))
        authority = rechain.text(self.repo, self.pin, rechain.AUTHORITY)
        anchor = '        "' + self.parent + '",'
        self.test.assertEqual(authority.count(anchor), 1)
        # Register only this additive successor, as production mechanical's
        # authority_edit permits. Preserve every guarded cleanup condition.
        self.write(rechain.AUTHORITY, authority.replace(
            anchor, '        "' + revision + '",\n' + anchor, 1))
        self.write(rechain.FINGERPRINT, 'stale-fixture-' + revision + '\n')
        return self.commit('fixture migration ' + revision)

    real_run = staticmethod(subprocess.run)

    def observe(self, argv, *args, **kwargs):
        # Observe subprocesses and preserve their real results. Never print env,
        # raw diagnostics, or the private helper's secret-bearing stdout.
        argv = list(argv)
        private = any(str(a).endswith('/scripts/private-pg.sh') for a in argv)
        refresh = any(str(a).endswith('scripts/refresh_genesis_fingerprint.sh') for a in argv)
        if private or refresh:
            env = kwargs.get('env', os.environ)
            for name in self.overrides:
                self.test.assertNotIn(name, env)
            cwd = Path(kwargs.get('cwd', self.repo))
            script = next(str(a) for a in argv if str(a).endswith('.sh'))
            path = (cwd / script).resolve()
            self.test.assertTrue(path.is_file())
            self.executions.append(dict(script=str(path),
                script_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                python=self.python, alembic=self.alembic, overrides_absent=True))
            argv = cpu_share.niced(argv)
        result = self.real_run(argv, *args, **kwargs)
        if refresh and result.returncode:
            report('rebuild-failure', dict(exit_code=result.returncode,
                diagnostics=[re.sub(r'postgres(?:ql)?://\S+', '[redacted endpoint]', line)
                             for line in rechain.stderr_tail(result.stderr)]))
        if private and 'loopback' in argv and result.returncode == 0:
            directory = Path(argv[argv.index('loopback') - 1])
            pid = (directory / 'data/postmaster.pid').read_text().splitlines()
            self.test.assertEqual((directory / 'data/PG_VERSION').read_text().strip(), '17')
            track_process(int(pid[0]))
            self.clusters.append(dict(directory=str(directory), pid=int(pid[0]), port=int(pid[3])))
        return result

    def assert_graph(self, candidate):
        self.test.assertEqual(rechain.routed_head(self.repo, candidate), 'invariant_2267_b')
        self.test.assertEqual(rechain.revision(rechain.text(self.repo, candidate,
            rechain.VERSIONS + 'invariant_2267_b.py')), ('invariant_2267_b', 'invariant_2267_a'))
        rechained = [e for e in self.events() if e['type'] == 'rechained']
        self.test.assertTrue(rechained)
        self.test.assertTrue(all(e['mechanical'] for e in rechained))
        report('rechain-graph', dict(candidate=candidate, head='invariant_2267_b',
            predecessor='invariant_2267_a', conflicts_resolved=True, receipts=rechained))

    def independent_rebuild(self, final):
        """Rebuild the exact committed tree on a separately owned real cluster."""
        wt = self.root / 'independent-tree'
        # Unix socket names have a platform length limit. Do not nest this
        # cluster beneath arbitrarily long fixture/worktree directories.
        pg_tmp = tempfile.TemporaryDirectory(prefix='pg-')
        self.test.addCleanup(pg_tmp.cleanup)
        pg = Path(pg_tmp.name)
        git(self.repo, 'worktree', 'add', '-q', '--detach', str(wt), final)
        self.test.addCleanup(lambda: git(self.repo, 'worktree', 'remove', '--force', str(wt))
                             if wt.exists() else None)
        env = rechain.child_env()
        env['PATH'] = str(Path(self.python).parent) + ':' + env.get('PATH', os.defpath)
        env['AS_ALEMBIC_BIN'] = self.alembic
        self.test.assertTrue(self.clusters, 'production rebuild must precede independent rebuild')
        env['PRIVATE_PG_AVOID_PORT'] = str(self.clusters[-1]['port'])
        for name in self.overrides:
            self.test.assertNotIn(name, env)
        # Register stop before startup, so even a malformed helper response is cleaned.
        def stop():
            if (pg / 'data/postmaster.pid').exists():
                command(['/usr/lib/postgresql/17/bin/pg_ctl', '-w', '-D',
                         str(pg / 'data'), '-m', 'fast', 'stop'], env=rechain.child_env())
            self.test.assertFalse((pg / 'data/postmaster.pid').exists())
        self.test.addCleanup(stop)
        started = command(['bash', str(SOURCE / 'scripts/private-pg.sh'), str(pg), 'loopback'],
                          cwd=wt, env=env, timeout=120)
        match = re.search(r"^ADMIN_DSN='(postgresql://[^'\n]+@127\.0\.0\.1:\d+/postgres)'$",
                          started, re.M)
        self.test.assertIsNotNone(match, 'private helper did not return owned loopback endpoint')
        env['AS_LOCAL_PG_ADMIN'] = match.group(1)
        output = self.root / 'independent-snapshot.txt'
        command(['bash', 'scripts/refresh_genesis_fingerprint.sh', '--out', str(output)],
                cwd=wt, env=env, timeout=600)
        committed, independent = rechain.blob(self.repo, final, rechain.FINGERPRINT), output.read_bytes()
        self.test.assertGreaterEqual(len(independent.splitlines()), 1000)
        self.test.assertEqual(committed, independent)
        self.test.assertIn(self.object_name.encode(), committed)
        self.test.assertIn(self.object_name.encode(), independent)
        digest = hashlib.sha256(independent).hexdigest()
        receipts = [e for e in self.events() if e['type'] == 'rechain-fingerprint'
                    and rechain.git(self.repo, 'merge-base', '--is-ancestor', e['sha'],
                                    final, check=False).returncode == 0
                    and rechain.blob(self.repo, e['sha'], rechain.FINGERPRINT) == committed]
        self.test.assertTrue(receipts, 'stale seed must cause a production fingerprint event')
        self.test.assertEqual(receipts[-1]['fingerprint_sha256'], digest)
        # Negative controls use actual fixture blobs; a stale/wrong-tree snapshot fails.
        for wrong in (self.b, self.pin):
            with self.test.assertRaises(AssertionError):
                self.test.assertEqual(rechain.blob(self.repo, wrong, rechain.FINGERPRINT), independent)
        # The refresh script has dropped its transient database. Use a new owned
        # DB and separate connections to observe committed DDL, never its connection.
        probe = '''
import os, pathlib, subprocess, sys
import psycopg2
from scratch_listing_authority_cleanup import cleanup_listing_authority_scratch
admin = os.environ['AS_LOCAL_PG_ADMIN']
name = 'genesis_fp_2267_' + str(os.getpid())
scratch = admin.rsplit('/', 1)[0] + '/' + name
conn = psycopg2.connect(admin); conn.autocommit = True
with conn.cursor() as cur:
    cur.execute('CREATE DATABASE ' + name)
try:
    target = subprocess.check_output([sys.executable,
        'scripts/resolve_alembic_deploy_target.py', '--manifest',
        'alembic_supabase/branch_isolation.json', '--versions-dir',
        'alembic_supabase/versions', '--scratch-union'], cwd='api', text=True).strip()
    child = dict(os.environ, SUPABASE_DB_URL_DIRECT=scratch)
    subprocess.run([os.environ['AS_ALEMBIC_BIN'], '-c', 'alembic_supabase.ini',
                    'upgrade', target], cwd='api', env=child, check=True,
                   capture_output=True, text=True)
    fresh = psycopg2.connect(scratch)
    with fresh.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='public' AND c.relname=%s AND c.relkind='r'", (sys.argv[1],))
        assert cur.fetchone()[0] == 1, 'committed migration object absent on fresh connection'
    fresh.close()
finally:
    cleanup_listing_authority_scratch(admin, scratch)
    with conn.cursor() as cur:
        cur.execute('DROP DATABASE ' + name + ' WITH (FORCE)')
    conn.close()
print('fresh-connection committed object count=1; guarded cleanup complete')
'''
        observed = command(cpu_share.niced([self.python, '-c', probe, self.object_name]),
                           cwd=wt, env=env, timeout=900)
        self.test.assertEqual(observed,
                             'fresh-connection committed object count=1; guarded cleanup complete')
        stop()
        self.test.assertGreaterEqual(len(self.clusters), 2)
        self.test.assertEqual(len({row['directory'] for row in self.clusters}), len(self.clusters))
        self.test.assertNotEqual(self.clusters[-1]['port'], self.clusters[-2]['port'])
        for row in self.clusters:
            self.test.assertFalse((Path(row['directory']) / 'data/postmaster.pid').exists())
        report('independent-rebuild', dict(final_tree=window_gate.tree_of(self.repo, final),
            fingerprint_sha256=digest, byte_count=len(independent), same_bytes=True,
            stale_and_wrong_tree_rejected=True, fresh_connection_object_count=1,
            commands=self.executions, clusters=self.clusters, cleanup_complete=True))


class AC2Tests(AC1Tests):
    # Reuse only isolation/teardown, not the driver's test selection.
    test_launch_matrix = None
    test_base_red_hold_and_both_release_keys = None

    def check_live(self):
        super().check_live()

    def test_rechained_queue_and_direct_matrix(self):
        cases = [('green', None), ('red', None), ('undetermined', None),
                 ('undetermined', 'missing'), ('undetermined', 'short'),
                 ('undetermined', 'other-tree'), ('green', 'complete'),
                 ('undetermined', 'stale')]
        for index, (state, cache) in enumerate(cases):
            with self.subTest(state=state, cache=cache):
                fixture = RebuildFixture(self.root / str(index), self)
                conflict = rechain.git(fixture.repo, 'merge-tree', '--write-tree',
                                       fixture.a, fixture.b, check=False)
                self.assertEqual(conflict.returncode, 1)
                self.assertIn(rechain.MANIFEST.encode(), conflict.stdout)
                clean = fixture.classify()
                self.assertTrue(clean['rechain_dry_run'])
                result, local, calls = fixture.run_window(state, queue=bool(index % 2), cache=cache)
                self.assertEqual(result, 0 if state == 'green' else 2)
                self.assertEqual(local == fixture.base, state != 'green')
                self.assertEqual(bool(calls), cache != 'complete')
                final = next(row['candidate'] for row in fixture.timeline if row['step'] == 'fingerprint')
                fixture.assert_graph(final)
                self.assertNotEqual(window_gate.tree_of(fixture.repo, final),
                                    git(fixture.repo, 'rev-parse', clean['merged_tree'] + '^{tree}'))
                self.assertIn(fixture.object_name.encode(), rechain.blob(fixture.repo, final, rechain.FINGERPRINT))
                report('AC2-rechain-case', dict(state=state, cache=cache,
                    commands=fixture.executions, clusters=fixture.clusters))
                fixture.stack.close()

    def test_changed_remainder_needs_its_own_scoped_verdict(self):
        import base_red
        for state in ('green', 'undetermined'):
            with self.subTest(state=state):
                fixture = WindowFixture(self.root / state, self)
                fixture.classify()
                bad_spec, bad_branch = 'L-spec-990003', 'l-spec-990003'
                path = 'dashboard/src/remainder.test.ts'
                git(fixture.repo, 'checkout', '-q', '-b', bad_branch, fixture.base)
                fixture.write(path, 'export const test_red = false;\n')
                bad = fixture.commit('fixture attributed failure')
                git(fixture.repo, 'checkout', '-q', 'master')
                fixture.extra_members.append((bad_spec, bad_branch, bad))
                (fixture.root / 'content' / (bad_spec + '.md')).write_text('Writes: ' + path + '\n')
                window_gate.ev('spec-written', bad_spec, branch=bad_branch,
                    file=str(fixture.root / 'content' / (bad_spec + '.md')))
                fixture.classify(bad_spec, bad_branch)
                # Hosted base collection is controlled; classify/ownership,
                # preparation, scope, judge and actual landing remain real.
                with patch.object(base_red, 'failing', return_value=dict(
                    sha=fixture.base, complete=True, undetermined=[],
                    kinds_covered=['pytest', 'pg-parity', 'vitest', 'suite-error'], ids={})):
                    result, local, calls = fixture.run_window(state, remainder=True)
                self.assertEqual(result, 1 if state == 'green' else 2)
                self.assertEqual(len(calls), 2)
                self.assertEqual([row['state'] for row in calls], ['red', state])
                self.assertNotEqual(calls[0]['tree'], calls[1]['tree'])
                self.assertEqual(local == fixture.base, state != 'green')
                drops = [e for e in fixture.events() if e['type'] == 'window-dropped']
                self.assertEqual([e['subject'] for e in drops], [bad_spec])
                if state == 'green':
                    self.assertEqual(window_gate.tree_of(fixture.repo, local), calls[1]['tree'])
                    self.assertEqual([e['subject'] for e in fixture.events()
                                      if e['type'] == 'shipped'], [fixture.spec])
                report('AC2-remainder', dict(state=state, result=result,
                    calls=calls, dropped=drops, local=local))
                fixture.stack.close()

    def test_ordinary_queue_and_direct_exact_tree_matrix(self):
        cases = [('green', None), ('red', None), ('undetermined', None),
                 ('undetermined', 'missing'),
                 ('undetermined', 'short'), ('undetermined', 'other-tree'),
                 ('green', 'complete'), ('undetermined', 'stale')]
        for queue in (False, True):
            for index, (state, cache) in enumerate(cases):
                with self.subTest(queue=queue, state=state, cache=cache):
                    fixture = WindowFixture(self.root / f'{queue}-{index}', self)
                    fixture.classify()
                    result, local, calls = fixture.run_window(state, queue=queue, cache=cache)
                    self.assertEqual(result, 0 if state == 'green' else 2)
                    self.assertEqual(local == fixture.base, state != 'green')
                    self.assertEqual(bool(calls), cache != 'complete')
                    self.assertLess(len(fixture.scoped), len(fixture.required))
                    fixture.stack.close()

    def test_policy_tree_and_compare_and_swap(self):
        for race in (False, True):
            with self.subTest(race=race):
                fixture = WindowFixture(self.root / str(race), self)
                git(fixture.repo, 'checkout', '-q', fixture.branch)
                fixture.write(quarantine_file.PATH,
                              'tests/test_expired.py::test_red\tfixture\t2000-01-01\texpired\n')
                fixture.candidate = fixture.commit('fixture policy input')
                fixture.grant.append(quarantine_file.PATH)
                fixture.metadata()
                git(fixture.repo, 'checkout', '-q', 'master')
                fixture.classify()
                dry_tree = window_gate.tree_of(fixture.repo, fixture.candidate)
                fixture.seed(dry_tree, fixture.required)
                if race:
                    result, local, calls = fixture.run_window(race=True)
                    self.assertEqual(result, 2)
                    self.assertFalse(any(e['type'] == 'shipped' for e in fixture.events()))
                    self.assertNotEqual(git(fixture.repo, 'rev-parse', 'master'), fixture.base)
                    report('AC2-CAS', dict(ref=git(fixture.repo, 'rev-parse', 'master'),
                                          timeline=fixture.timeline, shipped=[]))
                else:
                    result, local, calls = fixture.run_window()
                    self.assertEqual(result, 0)
                    self.assertEqual(len(calls), 1)
                    self.assertNotEqual(calls[0]['tree'], dry_tree)
                    self.assertEqual(calls[0]['tree'], window_gate.tree_of(fixture.repo, local))
                fixture.stack.close()


class AC3Tests(AC1Tests):
    test_launch_matrix = None
    test_base_red_hold_and_both_release_keys = None

    def test_real_rechain_and_independent_fingerprint(self):
        fixture = RebuildFixture(self.root / 'rebuild', self)
        conflict = rechain.git(fixture.repo, 'merge-tree', '--write-tree',
                               fixture.a, fixture.b, check=False)
        self.assertEqual(conflict.returncode, 1)
        self.assertIn(rechain.MANIFEST.encode(), conflict.stdout)
        merged, reason = rechain.resolve(fixture.repo, fixture.a,
            (fixture.spec, fixture.branch, fixture.b), window_id('AC3'), ev=window_gate.ev)
        self.assertIsNone(reason)
        self.assertIsNotNone(merged)
        fixture.assert_graph(merged)
        final = rechain.fingerprint(fixture.repo, merged, window_id('AC3'),
                                    ev=window_gate.ev, project='albert-scott')
        self.assertNotEqual(final, merged)
        fixture.assert_graph(final)
        # recorded_fingerprint already compared the real independent rebuild.
        self.assertEqual(rechain.blob(fixture.repo, final, rechain.FINGERPRINT),
                         RebuildFixture.recorded['content'])
        self.assertEqual(git(fixture.repo, 'rev-parse', 'master'), fixture.a)
        fixture.stack.close()


class AC4Tests(AC1Tests):
    test_launch_matrix = None
    test_base_red_hold_and_both_release_keys = None

    def test_moved_base_rebuild_requires_fresh_exact_tree_scoped_green(self):
        for index, (state, short_cache) in enumerate(
                [('green', False), ('red', False), ('undetermined', False), ('green', True)]):
            with self.subTest(state=state, short_cache=short_cache):
                fixture = RebuildFixture(self.root / str(index), self, moved=True)
                fixture.classify()
                initial = []

                def hosted(repo, sha, required, window, **kwargs):
                    wanted = window_gate.scope(repo, fixture.pin, sha, fixture.required)['checks']
                    self.assertEqual(required, wanted)
                    initial.append(dict(candidate=sha, tree=window_gate.tree_of(repo, sha),
                                        checks=required, state='green'))
                    fixture.timeline.append(dict(step='T0-verify', **initial[-1]))
                    return dict(state='green', nodes=[], url='', doc={})

                with patch.object(window_gate, 'verify', side_effect=hosted):
                    result = window_gate.main_([window_id('initial'), '--project', 'albert-scott',
                        '--specs', fixture.spec, '--rehearse', '--on', fixture.pin])
                self.assertEqual(result, 0)
                self.assertEqual(len(initial), 1)
                self.assertEqual(git(fixture.repo, 'rev-parse', 'master'), fixture.pin)
                t0 = initial[0]['tree']
                fixture.seed(t0, initial[0]['checks'])
                # A real competing commit lands between rehearsal and window
                # assembly; the managed window must build on this new master.
                git(fixture.repo, 'update-ref', 'refs/heads/master', fixture.a, fixture.pin)
                git(fixture.repo, 'push', '-q', 'origin', 'master')
                fixture.base = fixture.a
                fixture.required = window_gate.required_checks(fixture.repo, fixture.base)
                fixture.timeline.append(dict(step='sibling-landed', candidate=fixture.a))
                result, local, calls = fixture.run_window(state, fingerprint_cache=short_cache)
                self.assertEqual(result, 0 if state == 'green' else 2)
                self.assertEqual(local == fixture.a, state != 'green')
                self.assertEqual(len(calls), 1, 'T0 or short T1 cache must not suppress fresh verify')
                final = calls[0]['candidate']
                t1 = calls[0]['tree']
                self.assertNotEqual(t0, t1)
                self.assertEqual(calls[0]['checks'], window_gate.scope(
                    fixture.repo, fixture.a, final, fixture.required)['checks'])
                self.assertEqual(git(fixture.repo, 'merge-base', fixture.a, final), fixture.a)
                fixture.assert_graph(final)
                self.assertEqual(rechain.blob(fixture.repo, final, rechain.FINGERPRINT),
                                 RebuildFixture.recorded['content'])
                steps = [row['step'] for row in fixture.timeline]
                self.assertLess(steps.index('T0-verify'), steps.index('sibling-landed'))
                self.assertLess(steps.index('sibling-landed'), steps.index('fingerprint'))
                self.assertLess(steps.index('fingerprint'), steps.index('verify'))
                if state == 'green':
                    self.assertLess(steps.index('verify'), steps.index('advance'))
                    self.assertEqual(window_gate.tree_of(fixture.repo, local), t1)
                else:
                    self.assertNotIn('advance', steps)
                report('AC4-moved-base', dict(state=state, short_cache=short_cache,
                    T0=t0, T1=t1, local=local, sibling=fixture.a, result=result,
                    required=fixture.required, scoped=calls[0]['checks'],
                    timeline=fixture.timeline))
                fixture.stack.close()


if __name__ == '__main__':
    # unittest fails unknown/empty selections. Turn any future skip into failure.
    class NoSkips(unittest.TextTestResult):
        def addSkip(self, test, reason):
            self.addFailure(test, (AssertionError, AssertionError('skip refused: ' + reason), None))

        def wasSuccessful(self):
            return self.testsRun > 0 and super().wasSuccessful()

    class NoSkipRunner(unittest.TextTestRunner):
        def __init__(self, *args, **kwargs):
            kwargs.update(resultclass=NoSkips, verbosity=2)
            super().__init__(*args, **kwargs)

    unittest.main(testRunner=NoSkipRunner)
