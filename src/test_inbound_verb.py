#!/usr/bin/env python3
"""Seeded equality and real entry-point/install checks for inbound (no live data)."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

REPO = Path(__file__).resolve().parent.parent
STAMP = '2026-11-01T00:00:00Z'

# Frozen pre-extraction formatter, independent of the new shared producer.
ORACLE = '''
def baseline(events):
    def line(r):
        base = (f"{r['source']} · {r['kind']} · registered {r['registered_at']} · "
                f"{r['attempts']} attempt(s)")
        return base + (f" · last_error: {r['last_error']}" if r.get('last_error') else '')
    carry_mod, carry_err = fold._carry()
    if carry_mod is None:
        rows = [f"unavailable — {carry_err}"]
    else:
        try:
            rows = [line(r) for r in carry_mod.uncarried(events)]
        except Exception as x:
            rows = [f"unavailable — carry.uncarried raised {type(x).__name__}: {x}"]
    intake_mod, intake_err = fold._intake()
    if intake_mod is None:
        rows.append(f"unavailable — {intake_err}")
    else:
        try:
            rows += intake_mod.board_rows(events)
        except Exception as x:
            rows.append(f"unavailable — intake.board_rows raised {type(x).__name__}: {x}")
    return rows
'''


class InboundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='inbound-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, HOME=str(self.root / 'home'),
                        DOIT_ROOT=str(self.root), DOIT_READ_CACHE='0',
                        DOIT_PROJECT='', DOIT_SESSIONS_DIR=str(self.root / 'sessions'),
                        V4_LEDGER_DIR=str(self.root / 'v4'),
                        V4_INBOX_DIR=str(self.root / 'inbox'),
                        V4_STAGING_DIR=str(self.root / 'staging'),
                        PYTHONPATH=str(REPO / 'src'))
        for name in ('events', 'v4', 'inbox', 'staging'):
            (self.root / name).mkdir()

    def code(self, code, **env):
        p = subprocess.run(['/usr/bin/python3', '-c', textwrap.dedent(code)],
                           cwd=REPO, env={**self.env, **env}, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr.decode() + p.stdout.decode())
        return p.stdout

    def events(self, actor, events):
        (self.root / 'events' / f'L-{actor}-0001.jsonl').write_text(
            ''.join(json.dumps(dict(v=1, ts=STAMP, **e)) + '\n' for e in events))

    def seed(self):
        pr = lambda n: f'https://github.com/example/repo/pull/{n}'
        self.events('intake', [dict(type='inbound-registered', source=pr(n))
                              for n in (1, 1, 2, 3, 4, 5)] + [
            dict(type='inbound-awaiting', subject=pr(n), author_login='Zoë', opened_at=STAMP)
            for n in (6, 7, 1)] + [dict(type='inbound-awaiting-gone', subject=pr(7))])
        self.events('executor', [dict(type='carry-failed', source=pr(2), error='échec ☃'),
                                dict(type='spec-carried', source=pr(3)),
                                dict(type='inbound-covered', source=pr(4), covered_by='L-spec-9999', actor='builder')])
        self.events('builder', [dict(type='inbound-covered', source=pr(5),
                                    covered_by='L-spec-9999', actor='executor')])
        for n in (11, 12, 13):
            (self.root / 'v4' / f'{n}-fixture.yml').write_text(
                'status: registered\nhanded_over_at: ' + STAMP + '\n')
        (self.root / 'inbox' / '11-fixture-spec.md').write_text('# staged\n')
        (self.root / 'staging' / 'fixture.md').write_text('# staging fallback\n')
        (self.root / 'v4' / '13-fixture.yml').write_text(
            'status: registered\nhanded_over_at: ' + STAMP + '\nspec_file: fixture.md\n')

    def test_row_matrix_and_board(self):
        self.seed()
        self.code(textwrap.dedent('''
            import fold, carry, intake
            from unittest.mock import patch
        ''') + ORACLE + textwrap.dedent('''
            events = fold.read_events(use_cache=False)
            expected = [
                '11 · v4 · registered 2026-11-01T00:00:00Z · 0 attempt(s)',
                '13 · v4 · registered 2026-11-01T00:00:00Z · 0 attempt(s)',
                'https://github.com/example/repo/pull/1 · pr · registered 2026-11-01T00:00:00Z · 0 attempt(s)',
                'https://github.com/example/repo/pull/2 · pr · registered 2026-11-01T00:00:00Z · 1 attempt(s) · last_error: échec ☃',
                'https://github.com/example/repo/pull/5 · pr · registered 2026-11-01T00:00:00Z · 0 attempt(s)',
                'https://github.com/example/repo/pull/6 · awaiting registration · Zoë · opened 2026-11-01T00:00:00Z']
            assert baseline(events) == expected, baseline(events)
            assert fold.inbound_rows(events) == expected
            board = fold.render(events, *fold.fold(events))
            section = board.split('## INBOUND')[1].split('## ')[0]
            assert [r for r in section.splitlines()[1:] if r] == ['  ' + r for r in expected]
            with patch.object(fold, 'inbound_rows', return_value=['shared sentinel']) as producer:
                assert '  shared sentinel\\n' in fold.render(events, *fold.fold(events))
                producer.assert_called_once_with(events)
            for provider, name in ((carry, 'uncarried'), (intake, 'board_rows')):
                with patch.object(provider, name, side_effect=ValueError('fixture ☃')):
                    assert fold.inbound_rows(events) == baseline(events)
                    assert len(fold.inbound_rows(events)) > 1
            for seam in ('_carry', '_intake'):
                with patch.object(fold, seam, return_value=(None, 'fixture absent')):
                    assert fold.inbound_rows(events) == baseline(events)
                    assert len(fold.inbound_rows(events)) > 1
            with patch.object(carry, 'uncarried', return_value=[]), patch.object(intake, 'board_rows', return_value=[]):
                assert fold.inbound_rows([]) == baseline([]) == []
        '''))

    def cli(self, direct=False, **env):
        args = ['/usr/bin/python3', str(REPO / 'src/fold.py'), 'inbound'] if direct else [str(REPO / 'doit'), 'inbound']
        p = subprocess.run(args, cwd=REPO, env={**self.env, **env}, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        return p.stdout

    def hooks(self, provider=''):
        hooks = self.root / 'hooks'
        hooks.mkdir(exist_ok=True)
        (hooks / 'sitecustomize.py').write_text('''
import sys, types, os
from pathlib import Path
# Call sentinels operate on script-mode __main__ as well as imported fold.
def trace(frame, event, arg):
    if event == 'call':
        name, file = frame.f_code.co_name, Path(frame.f_code.co_filename).name
        if (file == 'fold.py' and name in ('fold', 'render', 'append', '_panes', '_notes', '_backup', '_relay_unserved')) or (file == 'dispatch.py' and name in ('emit', 'main', 'dispatch')):
            raise AssertionError('forbidden ' + file + ':' + name)
    return trace
sys.settrace(trace)
if os.environ.get('FAIL_PROVIDER'):
    import importlib
    module = importlib.import_module(os.environ['FAIL_PROVIDER'])
    def fail(*args): raise ValueError('fixture unavailable ☃')
    setattr(module, 'uncarried' if module.__name__ == 'carry' else 'board_rows', fail)
''')
        return dict(PYTHONPATH=str(hooks) + os.pathsep + str(REPO / 'src'), FAIL_PROVIDER=provider)

    def test_cli_entrypoints_bytes_and_degradation(self):
        self.seed()
        expected = self.code('import fold\n' + ORACLE + '\nprint("".join("  " + r + "\\n" for r in baseline(fold.read_events(use_cache=False))), end="")')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        hooks = self.hooks()
        for direct in (False, True):
            self.assertEqual(self.cli(direct, **hooks), expected)
            for provider in ('carry', 'intake'):
                degraded = self.cli(direct, **self.hooks(provider))
                self.assertIn(b'unavailable', degraded)
                self.assertIn('☃'.encode(), degraded)
                self.assertIn(b'https://', degraded)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        help_output = subprocess.check_output([str(REPO / 'doit'), 'help'], env=self.env)
        self.assertIn(b'doit inbound', help_output)

    def test_cli_empty(self):
        for direct in (False, True):
            self.assertEqual(self.cli(direct, **self.hooks()), b'')

    def test_cli_real_cache_roundtrip(self):
        self.seed()
        events = {p: p.read_bytes() for p in (self.root / 'events').iterdir()}
        before = set(self.root.rglob('*'))
        expected = self.cli(**self.hooks())
        self.assertEqual(self.cli(**self.hooks(), DOIT_READ_CACHE='1'), expected)
        self.assertEqual(self.cli(True, **self.hooks(), DOIT_READ_CACHE='1'), expected)
        self.code('''
            import fold
            events = fold.read_events()
            covered = {e['source']: e['actor'] for e in events if e['type'] == 'inbound-covered'}
            assert covered['https://github.com/example/repo/pull/4'] == 'executor'
            assert covered['https://github.com/example/repo/pull/5'] == 'builder'
            import sqlite3, os
            from pathlib import Path
            databases = list(Path(os.environ['DOIT_ROOT']).rglob('*.sqlite*'))
            assert databases
            with sqlite3.connect(databases[0]) as con:
                assert con.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        ''', DOIT_READ_CACHE='1')
        self.assertEqual({p: p.read_bytes() for p in events}, events)
        added = [p.relative_to(self.root).as_posix() for p in set(self.root.rglob('*')) - before if p.is_file()]
        self.assertTrue(added, added)
        self.assertTrue(all(p.startswith(('cache/', 'hooks/')) for p in added), added)

    def test_auto_carry_shell_selection_guards_and_arguments(self):
        script = REPO / 'scripts/auto-carry.sh'
        self.assertTrue(os.access(script, os.X_OK))
        subprocess.run(['/bin/bash', '-n', str(script)], check=True)
        work = self.root / 'repo'
        (work / 'scripts').mkdir(parents=True)
        # Redirect only host destinations; shell selection and carry logic are untouched.
        text = script.read_text().replace('/tmp/doit-auto-carry.lock', str(self.root / 'lock')).replace('/home/albert/.do-it/bin/auto-carry.skip', str(self.root / 'skip'))
        target = work / 'scripts/auto-carry.sh'
        target.write_text(text)
        target.chmod(0o755)
        rows = ['  11 · v4 · registered now · 0 attempt(s)',
                '  https://github.com/o/r/pull/9 · pr · registered now · 0 attempt(s)',
                '  skipped · v4 · registered now · 0 attempt(s)',
                '  tried · pr · registered now · 2 attempt(s) · last_error: no',
                '  waiting · awaiting registration', '  unavailable — fixture',
                '  $(touch${IFS}PWNED) · v4 · registered now · 0 attempt(s)']
        (self.root / 'rows').write_text('\n'.join(rows) + '\n')
        (self.root / 'skip').write_text('skipped explanation\n')
        stub = '''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
r = Path(os.environ['FIXTURE'])
with (r / 'calls').open('a') as f: f.write(json.dumps([os.environ.get('DOIT_LEDGER_FILE'), *sys.argv[1:]]) + '\\n')
if sys.argv[1] == 'inbound': print((r / 'rows').read_text(), end='')
'''
        (work / 'doit').write_text(stub)
        (work / 'doit').chmod(0o755)
        bins = self.root / 'bin'
        bins.mkdir()
        gh = bins / 'gh'
        gh.write_text('''#!/usr/bin/python3
import base64, sys
args = sys.argv[1:]
if args[0] == 'api': print(base64.b64encode(b'project: do-it-v2\\n# fixture').decode())
elif 'title' in args: print('Title $(touch PWNED) ☃')
elif 'headRefOid' in args: print('fixture-ref')
else: print('spec.md')
''')
        gh.chmod(0o755)
        flock = bins / 'flock'
        flock.write_text('#!/bin/sh\nexit "${LOCK_DENIED:-0}"\n')
        flock.chmod(0o755)
        env = dict(self.env, FIXTURE=str(self.root), PATH=str(bins) + os.pathsep + os.environ['PATH'])
        p = subprocess.run([str(target)], cwd=work, env=env, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        calls = [json.loads(l) for l in (self.root / 'calls').read_text().splitlines()]
        self.assertEqual(calls[0], ['L-executor-0001.jsonl', 'inbound'])
        selected = subprocess.check_output(['/bin/bash', '-c', "sed -n '/^## INBOUND/,/^## /p' | grep ' 0 attempt' | awk '{print $1}'"], input=('## INBOUND\n' + '\n'.join(rows) + '\n## NEXT\n').encode()).decode().splitlines()
        self.assertEqual([c[2] for c in calls[1:]], [s for s in selected if s != 'skipped'])
        self.assertEqual(calls[1], ['L-executor-0001.jsonl', 'carry', '11', '--repo', '/opt/albert-scott', '--project', 'albert-scott'])
        self.assertEqual(calls[2], ['L-executor-0001.jsonl', 'carry', 'https://github.com/o/r/pull/9', '--repo', '/home/albert/do-it-v2', '--project', 'do-it-v2', '--title', 'Title $(touch PWNED) ☃', '--body', 'project: do-it-v2\n# fixture'])
        self.assertFalse((work / 'PWNED').exists())
        before = (self.root / 'calls').read_bytes()
        subprocess.run([str(target)], env=dict(env, LOCK_DENIED='1'), check=True)
        self.assertEqual((self.root / 'calls').read_bytes(), before)

    def test_auto_carry_install_and_machine_drift(self):
        self.code('''
            import os, pathlib, re, shutil
            from unittest.mock import patch
            import crons, fold, install_sync, machine, models
            root = fold.ROOT
            models.PATH = root / 'default-models/models.toml'
            repo = pathlib.Path.cwd()
            manifest = repo / 'crons.toml'
            raw = next(r for r in crons.rows() if r['name'] == 'auto-carry')
            assert raw['schedule'] == '7 * * * *' and raw['where'] == 'user'
            assert raw['owner'] == 'executor' and raw['project'] == 'do-it-v2'
            assert raw['max_runtime_s'] is None
            line = crons.print_line('auto-carry')
            assert line.endswith(' >> ' + str(root) + '/logs/auto-carry.log 2>&1  # doit-cron:auto-carry')
            assert ' cron-run auto-carry -- ' + str(repo / 'scripts/auto-carry.sh') in line
            legacy = next(l for l in (repo / 'machine/crontab.txt').read_text().splitlines() if l.startswith('7 ') and '/bin/auto-carry.sh' in l)
            assert re.search(raw['sig'], legacy) and re.search(raw['sig'], line)
            assert not re.search(raw['sig'], '/scripts/unrelated.sh')
            fixture = root / 'fixture-repo'
            (fixture / 'machine').mkdir(parents=True)
            models = '[fixture]\\nmodel = "local"\\n'
            saved = '# pending Thinker reconciliation\\n' + legacy + '\\n'
            (fixture / 'machine/models.toml').write_text(models)
            (fixture / 'machine/crontab.txt').write_text(saved)
            (root / 'models.toml').write_text(models)
            tab = root / 'crontab'
            previous = '# old pre-image\\n' + legacy + '\\n'
            tab.write_text(previous)
            marker = root / 'machine-last'
            marker.mkdir()
            (marker / 'models.toml').write_text(models)
            (marker / 'crontab.txt').write_text(previous)
            assert install_sync.install_machine(fixture, root=root, crontab_path=tab) == ['crontab.txt']
            assert tab.read_text() == saved
            assert (root / 'crontab.txt.bak').read_text() == previous
            dirty = saved + '# local change\\n'
            tab.write_text(dirty)
            try: install_sync.install_machine(fixture, root=root, crontab_path=tab)
            except install_sync.MachineInstallRefused: pass
            else: raise AssertionError('must refuse divergent live')
            assert tab.read_text() == dirty
            unrelated = '# keep comment\\n1 * * * * /bin/true\\n'
            commented = '# ' + legacy + '\\n'
            original = unrelated + legacy + '\\n' + legacy + '\\n' + commented
            tab.write_text(original)
            events = [dict(type='install-synced', sha='previous', ts='2026-10-01T00:00:00Z'),
                      dict(type='shipped', project='do-it-v2', sha='fixture', ts='2026-11-01T00:00:00Z')]
            with patch.object(install_sync, 'changed_rows', return_value=['auto-carry']), patch.object(install_sync, 'sync_realm'):
                assert install_sync.run(events, root=root, repo=fixture, manifest=manifest, crontab_path=tab) == ['auto-carry']
            # Independent reopen verifies the real install, including duplicate replacement.
            installed = tab.read_bytes().decode()
            assert installed == unrelated + line + '\\n' + commented, installed
            assert crons.install('auto-carry', crontab_path=tab)['action'] == 'replaced'
            assert tab.read_bytes().decode() == installed
            crond = root / 'cron.d'
            crond.mkdir()
            assert not [r for r in crons.check(crontab_text=installed, crond_dir=crond) if r['name'] == 'auto-carry']
            assert not [r for r in crons.unwrapped(crontab_text=installed, crond_dir=crond) if r['name'] == 'auto-carry']
            drift = machine.check(live_models=models, live_crontab=installed, repo=fixture)
            assert [(d.kind, d.subject) for d in drift] == [('differs', 'crontab.txt')]
            with patch.object(install_sync, '_crons_toml_at', side_effect=[{}, {'auto-carry': ('7 * * * *', line)}]):
                assert install_sync.changed_rows(repo, 'removed-fixture') == []
            assert tab.read_text() == installed  # removing a manifest row does not uninstall it
            tab.write_bytes(original.encode())  # scratch operator restore/read-back
            assert tab.read_bytes() == original.encode()
        ''')


if __name__ == '__main__':
    unittest.main()
