"""Hermetic sweep fixtures: real Git, subprocesses and production ledger writes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_rechain import Fixture, git, response
import fold
import preflight_sweep as sweep
import rechain
import rechain_check as stacker

HERE = Path(__file__).resolve().parent.parent
PYTEST = '/opt/albert-scott/.venv/bin/python'
PROJECT = 'albert-scott'


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sweep-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = PROJECT
        self.fx = Fixture(self.root)
        self.repo = self.fx.repo
        self.state = self.root / 'state'
        (self.state / 'content').mkdir(parents=True)
        (self.state / 'events').mkdir()
        (self.state / 'repos').mkdir()
        (self.state / 'repos' / PROJECT).symlink_to(self.repo, target_is_directory=True)
        self.guard_file = self.root / 'guards.txt'
        self.guard_file.write_text('tests/test_guard.py\n')
        self.fx.write('tests/test_guard.py', 'def test_guard(): assert True\n')
        self.fx.write('tests/test_old.py', 'def test_old(): assert True\n')
        self.fx.write('src/test_old.py', 'assert True\n')
        self.fx.write('api/tests/test_old.py', 'def test_old(): assert True\n')
        self.fx.write('dashboard/src/old.test.ts', 'old\n')
        isolation=(self.repo/rechain.TEST).read_text()
        self.fx.write(rechain.TEST, '''import json
from pathlib import Path
head=json.loads((Path(__file__).parents[1]/'alembic_supabase/branch_isolation.json').read_text())['heads'][0]['revision']
declared_heads=actual_heads={head}
MANIFEST=VERSIONS=None
def resolve_scratch_target(*args): return head
def resolve_target(*args): return head
def resolve_scratch_union_heads(*args): return (head,)
'''+isolation)
        self.base = self.fx.commit('guard base')
        self.fx.base = self.base
        self.addCleanup(patch.stopall)
        patch.object(fold, 'ROOT', self.state).start()
        patch.object(fold, 'EVENTS', self.state / 'events').start()
        config = self.state / 'grading.toml'
        config.write_text('[project.albert-scott]\nvenv = "/opt/albert-scott/.venv"\n[project.do-it-v2]\nvenv = "/opt/albert-scott/.venv"\n')
        patch.dict(os.environ, DOIT_ROOT=str(self.state), DOIT_LEDGER_FILE='L-builder-fixture.jsonl',
                   DOIT_GRADING_TOML=str(config), DOIT_PREFLIGHT_GUARDS=str(self.guard_file)).start()

    def entry(self, spec, changes, grant=None, parent=None):
        git(self.repo, 'checkout', '-B', 'br-'+spec, parent or self.base)
        for path, body in changes.items():
            if body is None:
                (self.repo / path).unlink()
            else:
                self.fx.write(path, body)
        sha = self.fx.commit(spec)
        git(self.repo, 'checkout', 'master')
        grant = list(changes) if grant is None else grant
        self.register(spec, 'br-'+spec, grant)
        return spec, 'br-'+spec, sha

    def register(self, spec, branch, grant):
        path = self.state / 'content' / (spec+'.md')
        if grant is not None:
            path.write_text('Writes:\n'+''.join('- '+p+'\n' for p in grant))
        fold.append(['spec-written', spec, 'project='+self.project], strict=True)
        # Gate events require their established executor actor.
        with patch.dict(os.environ, DOIT_LEDGER_FILE='L-executor-fixture.jsonl'):
            fold.append(['merge-gate-clean', spec, 'project='+self.project, 'branch='+branch], strict=True)

    def run_entries(self, entries, **kw):
        with patch.object(tempfile,'tempdir',str(self.root)):
            return sweep.run(self.repo, self.base, [e[0] for e in entries], **kw)

    def assert_red(self, entries, cls, **kw):
        result = self.run_entries(entries, **kw)
        self.assertFalse(result.ok)
        self.assertFalse(result.undetermined, result.findings)
        self.assertEqual(result.findings[-1]['class'], cls, result.findings)
        self.assertNotIn('guards', result.trace)
        self.assertNotIn('changed-tests', result.trace)
        return result

    def test_ac2_writes_overlap_and_graph(self):
        cases = [
            ({'outside.txt': 'changed'}, ['other.txt'], 'writes'),
            ({'outside.txt': 'changed'}, [], 'writes'),
            ({'outside.txt': 'changed'}, None, 'writes'),
            ({rechain.VERSIONS+'bad.py': 'revision=object()\ndown_revision=None\n'}, 'own', 'alembic-heads'),
            ({rechain.VERSIONS+'bad.py': "revision='b'\ndown_revision='missing'\n"}, 'own', 'alembic-heads'),
            ({rechain.VERSIONS+'bad.py': "revision='b'\ndown_revision=None\n"}, 'own', 'alembic-heads'),
            ({rechain.VERSIONS+'h0.py': "revision='h0'\ndown_revision='h0'\n"}, 'own', 'alembic-heads'),
        ]
        for i, (changes, grant, cls) in enumerate(cases):
            e = self.entry('case'+str(i), changes, list(changes) if grant == 'own' else grant)
            if grant is None:
                (self.state/'content'/(e[0]+'.md')).unlink()
            result = self.assert_red([e], cls)
            self.assertEqual(result.failed, e[0])
            response('AC2', dict(case=i, classification=cls, trace=result.trace))
        a = self.entry('left', {'app/a.py': 'a=1\n'}, ['app/'])
        b = self.entry('right', {'app/b.py': 'b=1\n'}, ['app/'])
        self.assert_red([a,b], 'overlap')

    def test_ac2_markers_and_unchanged_base(self):
        for i, trigger in enumerate(('short_transactions_scratch', 'ScratchRequired', 'AS_LOCAL_PG_ADMIN')):
            path = 'tests/test_marker'+str(i)+'.py'
            e = self.entry('marker'+str(i), {path: '# '+trigger+'\ndef test_ok(): pass\n'})
            self.assert_red([e], 'pg-marker')
            e = self.entry('marked'+str(i), {path: '# pg_scratch '+trigger+'\ndef test_ok(): pass\n'})
            self.assertTrue(self.run_entries([e]).ok)
        # An inherited violation is not a newly introduced violation.
        e = self.entry('inherited', {'tests/test_inherited.py': '# ScratchRequired\ndef test_ok(): pass\n'})
        self.base = e[2]
        later = self.entry('later', {'outside.txt': 'later\n'}, parent=self.base)
        self.assertTrue(self.run_entries([later]).ok)

    def test_ac2_resolver_is_only_overlap_exception(self):
        a, b = self.fx.branch('a'), self.fx.branch('b')
        for spec, branch, sha in (a,b):
            paths = git(self.repo, 'diff', '--name-only', self.base, sha).splitlines()
            self.register(spec, branch, paths)
        entries = stacker.resolve_specs(self.repo, ['a','b'])
        steps = stacker.stack(self.repo, self.base, entries)
        findings, _ = sweep.static(self.repo, self.base, entries, steps)
        self.assertFalse(findings)
        self.assertEqual(steps[-1].outcome, 'rechained')
        steps[-1].metadata.clear()
        self.assertEqual(sweep.static(self.repo, self.base, entries, steps)[0][-1]['class'], 'overlap')

    def test_ac3_inventory_failfast_and_real_runner(self):
        inventory, exclusion = sweep.guard_inventory(HERE/'lane/guard-tests.txt')
        self.assertEqual(len(inventory), 29)
        self.assertEqual(len(set(inventory)), 29)
        self.assertEqual(hashlib.sha256(('\n'.join(inventory)+'\n').encode()).hexdigest(),
                         'd7c787e27ddcbc6c534358f9de7d0ed2083090f24f5c6c725ce8b8c52c52a962')
        self.assertEqual(exclusion[0], 'tests/test_795_cron_pythonpath_parity.py::test_ac10_cron_lint_clean_over_every_committed_cron_template')
        self.assertIn('scripts/ci/cron_lint.sh', exclusion[1])
        self.assertEqual(sweep.GUARDS, HERE/'lane/guard-tests.txt')
        self.assertFalse((HERE/'scripts/rehearsal/guard-tests.txt').exists())
        print('AC3 inventory', json.dumps(inventory), 'exclusion', json.dumps(exclusion))
        e = self.entry('pass', {'outside.txt': 'new\n'})
        result = self.run_entries([e])
        self.assertTrue(result.ok, result.findings)
        self.assertEqual(result.trace, ['stack','static','guards','changed-tests'])
        self.assertEqual((result.project, result.guards), (PROJECT, 1))
        for body, undetermined in [('def test_guard(): assert False\n', False),
                                   ('import missing_fixture_package\n', True),
                                   ('import pytest\n@pytest.mark.skip\ndef test_guard(): pass\n', True)]:
            e = self.entry('guard'+str(len(body)), {'tests/test_guard.py': body, 'tests/test_later.py': 'raise RuntimeError("must not run")\n'})
            result = self.run_entries([e])
            self.assertFalse(result.ok)
            self.assertEqual(result.undetermined, undetermined, result.findings)
            self.assertEqual(result.stage, 'guards')
            self.assertNotIn('changed-tests', result.trace)
            if not undetermined:
                self.assertEqual(result.findings[-1]['class'], 'guard')
                self.assertEqual(result.failed, 'tests/test_guard.py::test_guard')
        self.guard_file.write_text('tests/test_missing.py\n')
        self.assertTrue(self.run_entries([e]).undetermined)
        calls = []
        with patch.object(sweep, 'command', side_effect=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv,0)):
            with patch.object(sweep, 'parse_junit', return_value=[('tests/test_guard.py::test_guard','tests/test_guard.py',False,False)]):
                sweep.pytest_stage(self.repo, ['tests/test_guard.py'], self.root, PYTEST, {}, deselect=['tests/test_guard.py::test_x'])
        self.assertEqual(calls[0][3:], ['-x','-p','no:cacheprovider','-o','addopts=','-o','junit_family=xunit1','--junitxml='+str(self.root/'results.xml'),
                                        '--deselect','tests/test_guard.py::test_x','--','tests/test_guard.py'])
        response('AC3', dict(inventory=inventory, real_pass=True, real_red=True, missing_and_skipped_held=True))

    def test_ac3_exclusion_directive_project_selection_and_policy(self):
        text = self.root/'inv.txt'
        text.write_text('# exclude: tests/test_a.py::test_slow | too slow\n\ntests/test_a.py\n')
        self.assertEqual(sweep.guard_inventory(text), (['tests/test_a.py'], ('tests/test_a.py::test_slow', 'too slow')))
        for bad in ('# exclude: tests/test_a.py::t\ntests/test_a.py\n',
                    '# exclude: tests/test_b.py::t | no file\ntests/test_a.py\n',
                    '# exclude: tests/test_a.py::t | r\n# exclude: tests/test_a.py::u | r\ntests/test_a.py\n',
                    'tests/test_a.py\ntests/test_a.py\n', '# nothing\n', '../x.py\n'):
            text.write_text(bad)
            with self.assertRaises(stacker.Undetermined, msg=bad):
                sweep.guard_inventory(text)
        self.assertEqual(sweep.guard_policy('do-it-v2'), ([], None))
        with self.assertRaises(stacker.Undetermined):
            sweep.guard_policy('unconfigured')
        self.assertEqual(len(sweep.guard_policy('albert-scott', HERE/'lane/guard-tests.txt')[0]), 29)

    def test_ac3_exclusion_is_deselected_and_printed(self):
        self.guard_file.write_text('# exclude: tests/test_guard.py::test_excluded | fixture reason\ntests/test_guard.py\n')
        self.fx.write('tests/test_guard.py', 'def test_guard(): pass\ndef test_excluded(): assert False\n')
        self.base = self.fx.commit('excluded node red')
        self.fx.base = self.base
        e = self.entry('excl', {'outside.txt': 'x\n'})
        result = self.run_entries([e])
        self.assertTrue(result.ok, result.findings)
        self.assertEqual(result.exclusion, ('tests/test_guard.py::test_excluded', 'fixture reason'))
        p = self.cli('preflight_sweep.py', e[0])
        self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
        self.assertIn('PREFLIGHT-GUARDS project=albert-scott guards=1 excluded=tests/test_guard.py::test_excluded reason=fixture reason', p.stdout)

    def test_ac3_guard_base_comparison(self):
        # Failure only on the candidate: red guard. Same failure on base: base-red, never blamed or clean.
        e = self.entry('cand-only', {'tests/test_guard.py': 'def test_guard(): assert False\n'})
        r = self.run_entries([e])
        self.assertEqual((r.ok, r.undetermined, r.findings[-1]['class']), (False, False, 'guard'))
        self.fx.write('tests/test_guard.py', 'def test_guard(): assert False\n')
        self.base = self.fx.commit('guard red on base')
        self.fx.base = self.base
        e = self.entry('both', {'outside.txt': 'x\n'})
        r = self.run_entries([e])
        self.assertEqual((r.ok, r.undetermined, r.reason, r.stage), (False, True, 'base-red', 'guards'))
        self.assertEqual((r.failed, r.findings[-1]['owner']), ('tests/test_guard.py::test_guard', '-'))
        self.assertNotIn('changed-tests', r.trace)
        p = self.cli('preflight_sweep.py', e[0])
        self.assertEqual(p.returncode, 2, p.stdout+p.stderr)
        self.assertIn('PREFLIGHT-UNDETERMINED guards base-red tests/test_guard.py::test_guard', p.stdout)
        # A guard node absent from base is candidate-introduced red.
        self.fx.write('tests/test_guard.py', 'def test_guard(): pass\n')
        self.base = self.fx.commit('guard green')
        self.fx.base = self.base
        e = self.entry('new-node', {'tests/test_guard.py': 'def test_guard(): pass\ndef test_other(): assert False\n'})
        r = self.run_entries([e])
        self.assertEqual((r.ok, r.undetermined, r.findings[-1]['class']), (False, False, 'guard'))

    def test_ac3_do_it_v2_guard_stage_is_empty(self):
        link = self.state/'repos'/PROJECT
        link.unlink()
        (self.state/'repos'/'do-it-v2').symlink_to(self.repo, target_is_directory=True)
        self.project = 'do-it-v2'
        e = self.entry('v2', {'src/test_real.py': 'import unittest\nclass T(unittest.TestCase):\n    def test_a(self): self.assertTrue(True)\nunittest.main() if __name__=="__main__" else None\n'})
        r = self.run_entries([e])
        self.assertTrue(r.ok, r.findings)
        self.assertEqual((r.project, r.guards, r.selected), ('do-it-v2', 0, ['src/test_real.py']))
        p = self.cli('preflight_sweep.py', e[0], project='do-it-v2')
        self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
        self.assertIn('PREFLIGHT-GUARDS project=do-it-v2 guards=0', p.stdout)
        self.assertIn('PREFLIGHT-CLEAN changed-tests', p.stdout)
        # An unconfigured project's policy is undetermined.
        (self.state/'repos'/'do-it-v2').unlink()
        (self.state/'repos'/'other').symlink_to(self.repo, target_is_directory=True)
        self.project = 'other'
        e = self.entry('other', {'outside.txt': 'o\n'})
        r = self.run_entries([e])
        self.assertTrue(r.undetermined and not r.ok and r.stage == 'guards', r.findings)

    def test_ac4_selection_deletion_and_empty(self):
        changes = {'tests/test_new.py': 'def test_new(): pass\n',
                   'tests/changed_test.py': 'def test_changed(): pass\n',
                   'api/tests/test_old.py': 'def test_old(): assert True\n# changed\n',
                   'src/test_new.py': 'assert True\n',
                   'dashboard/src/nested/new.test.tsx': 'test\n',
                   'tests/test_old.py': None, 'docs/readme.txt': 'text\n'}
        a = self.entry('selection', changes)
        steps = stacker.stack(self.repo, self.base, [a])
        selected, excluded = sweep.targets(self.repo, self.base, steps[-1].tip, PROJECT)
        self.assertEqual(selected, sorted(p for p in changes if p not in ('tests/test_old.py','docs/readme.txt','src/test_new.py')))
        v2_selected, _ = sweep.targets(self.repo, self.base, steps[-1].tip, 'do-it-v2')
        self.assertIn('src/test_new.py', v2_selected)
        self.assertEqual(excluded, {'docs/readme.txt': 'not a changed test', 'tests/test_old.py':'deleted', 'src/test_new.py':'not a changed test'})
        print('AC4 selected',json.dumps(selected),'excluded',json.dumps(excluded))
        e = self.entry('empty', {'outside.txt':'new\n'})
        result = self.run_entries([e])
        self.assertTrue(result.ok)
        self.assertEqual(result.selected, [])
        self.assertIn('empty', result.findings[-1]['detail'])
        for changes in ({'tests/test_changed.py': 'def test_bad(): assert False\n'},
                        {'tests/test_changed.py': 'import missing_fixture_package\n'}):
            e = self.entry('failure'+str(len(str(changes))), changes)
            result = self.run_entries([e])
            self.assertFalse(result.ok)
            self.assertEqual(result.stage, 'changed-tests')
            self.assertEqual(result.findings[-1]['class'], 'changed-test')
            self.assertEqual(result.failed, e[0])
        a = self.entry('multi-a', {'tests/test_a.py':'def test_a(): pass\n'})
        b = self.entry('multi-b', {'tests/test_b.py':'def test_b(): pass\n'})
        result = self.run_entries([a,b])
        self.assertTrue(result.ok, result.findings)
        self.assertEqual(result.selected, ['tests/test_a.py','tests/test_b.py'])
        worktree=self.root/'linked-worktree'
        git(self.repo,'worktree','add','--detach',str(worktree),self.base)
        try:
            self.assertEqual(stacker.project_for(worktree),PROJECT)
            self.assertTrue(sweep.run(worktree,self.base,[a[0]]).ok)
        finally:
            git(self.repo,'worktree','remove','--force',str(worktree))
        owners = {'a': {'tests/test_shared.py'}, 'b': {'tests/test_shared.py'}}
        self.assertEqual(sweep.owner(['tests/test_shared.py'], owners), '-')
        response('AC4', dict(selected=selected, excluded=excluded, empty=True, unique_and_shared_ownership=True))

    def use_v2(self):
        (self.state/'repos'/PROJECT).unlink()
        (self.state/'repos'/'do-it-v2').symlink_to(self.repo, target_is_directory=True)
        self.project = 'do-it-v2'

    def test_ac4_do_it_v2_scripts_multi_spec_and_base_red(self):
        self.use_v2()
        ok = 'import unittest\nclass T(unittest.TestCase):\n    def test_a(self): self.assertTrue(True)\nif __name__=="__main__": unittest.main()\n'
        a = self.entry('v2-a', {'src/test_a.py': ok})
        b = self.entry('v2-b', {'src/test_b.py': ok})
        r = self.run_entries([a])
        self.assertTrue(r.ok, r.findings)
        self.assertEqual((r.guards, r.selected, r.trace), (0, ['src/test_a.py'], ['stack','static','guards','changed-tests']))
        r = self.run_entries([a, b])
        self.assertTrue(r.ok, r.findings)
        self.assertEqual(r.selected, ['src/test_a.py', 'src/test_b.py'])
        bad = self.entry('v2-bad', {'src/test_bad.py': 'raise AssertionError("fixture")\n'})
        r = self.run_entries([bad])
        self.assertEqual((r.ok, r.undetermined, r.stage, r.findings[-1]['class'], r.failed), (False, False, 'changed-tests', 'changed-test', 'v2-bad'))
        # A changed script that already fails on base is undetermined base-red, never blamed.
        self.fx.write('src/test_bad.py', 'raise AssertionError("fixture")\n')
        self.base = self.fx.commit('script red on base')
        self.fx.base = self.base
        touched = self.entry('v2-touch', {'src/test_bad.py': 'raise AssertionError("fixture")\n# touched\n'})
        r = self.run_entries([touched])
        self.assertEqual((r.ok, r.undetermined, r.reason, r.failed, r.findings[-1]['owner']), (False, True, 'base-red', 'src/test_bad.py', '-'))

    def test_ac4_changed_pytest_base_red_and_candidate_only(self):
        self.fx.write('tests/test_changed.py', 'def test_x(): assert False\n')
        self.base = self.fx.commit('red on base')
        self.fx.base = self.base
        e = self.entry('touch', {'tests/test_changed.py': 'def test_x(): assert False\n# touched\n'})
        r = self.run_entries([e])
        self.assertEqual((r.ok, r.undetermined, r.reason, r.failed), (False, True, 'base-red', 'tests/test_changed.py::test_x'))
        self.assertEqual(r.findings[-1]['owner'], '-')
        e2 = self.entry('both', {'tests/test_changed.py': 'def test_x(): assert False\n# one\n'})
        e3 = self.entry('shared', {'tests/test_other.py': 'def test_y(): assert False\n'})
        r = self.run_entries([e3])
        self.assertEqual((r.undetermined, r.findings[-1]['class'], r.failed), (False, 'changed-test', e3[0]))

    MARKED = (
        'import os, uuid\nimport psycopg2, pytest\n'
        'pytestmark = pytest.mark.pg_scratch\n'
        'def test_scratch_row_read_back_through_second_connection():\n'
        '    dsn = os.environ["AS_LOCAL_PG_ADMIN"]\n'
        '    assert "127.0.0.1" in dsn and os.environ["SUPABASE_DB_URL"] == dsn\n'
        '    assert not os.environ.get("INHERITED_LIVE_DSN")\n'
        '    key = uuid.uuid4().hex\n'
        '    writer = psycopg2.connect(dsn)\n'
        '    cur = writer.cursor()\n'
        '    cur.execute("create table if not exists fixture_rows(k text)")\n'
        '    cur.execute("insert into fixture_rows values (%s)", (key,))\n'
        '    writer.commit()\n'
        '    reader = psycopg2.connect(dsn)\n'
        '    rc = reader.cursor()\n'
        '    rc.execute("select k from fixture_rows where k=%s", (key,))\n'
        '    assert rc.fetchall() == [(key,)]\n'
        '    writer.close(); reader.close()\n')

    def test_ac4_marked_test_runs_on_scratch_postgres(self):
        with patch.dict(os.environ, INHERITED_LIVE_DSN='postgresql://live.invalid/prod', SUPABASE_DB_URL='postgresql://live.invalid/prod'):
            e = self.entry('marked', {'tests/test_scratch_pg.py': self.MARKED})
            r = self.run_entries([e])
        self.assertTrue(r.ok, r.findings)
        self.assertEqual(r.selected, ['tests/test_scratch_pg.py'])
        self.assertFalse(list(self.root.glob('preflight-*')))
        # Without a scratch cluster the same test cannot pass: provisioning failure is undetermined.
        with patch.object(sweep, 'scratch_env', side_effect=stacker.Undetermined('scratch Postgres provisioning unavailable')):
            r = self.run_entries([e])
        self.assertTrue(r.undetermined and not r.ok)
        self.assertEqual(r.reason, 'scratch Postgres provisioning unavailable')
        # A marked failure on both trees compares against a separate clean instance.
        red = 'import pytest\npytestmark = pytest.mark.pg_scratch\ndef test_db(): assert False\n'
        self.fx.write('tests/test_scratch_red.py', red)
        self.base = self.fx.commit('marked red on base')
        self.fx.base = self.base
        e = self.entry('marked-red', {'tests/test_scratch_red.py': red+'# touched\n'})
        r = self.run_entries([e])
        self.assertEqual((r.undetermined, r.reason, r.failed), (True, 'base-red', 'tests/test_scratch_red.py::test_db'))

    def test_ac4_marked_provisioning_counts_inside_deadline(self):
        e = self.entry('marked-slow', {'tests/test_scratch_pg.py': self.MARKED})
        real = sweep.scratch_env
        def slow(*args, **kw):
            time.sleep(3)
            return real(*args, **kw)
        with patch.object(sweep, 'scratch_env', side_effect=slow):
            r = self.run_entries([e], budget_s=2)
        self.assertTrue(r.undetermined and not r.ok)
        self.assertLess(r.wall_s, 4)
        self.assertFalse(list(self.root.glob('preflight-*')))

    def test_ac4_dashboard_command(self):
        (self.repo/'dashboard/node_modules/.bin').mkdir(parents=True)
        runner = self.repo/'dashboard/node_modules/.bin/vitest'
        runner.write_text('#!/bin/sh\nexit 0\n')
        runner.chmod(0o755)
        self.fx.write('dashboard/package.json', '{"scripts":{"test":"vitest run"}}')
        commands=[]
        with patch.object(sweep, 'command', side_effect=lambda argv, **kw: commands.append((argv, kw)) or subprocess.CompletedProcess(argv,0)):
            sweep.dashboard(self.repo, ['dashboard/src/old.test.ts'], {})
        argv = commands[0][0]
        self.assertEqual(argv[:5], ['npm','test','--','--bail=1','--reporter=json'])
        self.assertTrue(argv[5].startswith('--outputFile=') and argv[6:] == ['src/old.test.ts'], argv)
        self.assertEqual(commands[0][1]['cwd'], self.repo/'dashboard')

    def test_ac5_budget_cleanup_cpu_and_process_group(self):
        e = self.entry('sleep', {'tests/test_sleep.py': 'import pathlib,subprocess,sys,time\ndef test_sleep():\n    p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\n    pathlib.Path('+repr(str(self.root/'pid'))+').write_text(str(p.pid))\n    time.sleep(60)\n'})
        before = git(self.repo,'worktree','list','--porcelain')
        result = self.run_entries([e], budget_s=12)
        self.assertFalse(result.ok)
        self.assertTrue(result.undetermined)
        self.assertLess(result.wall_s, 14)
        self.assertGreater(result.cpu_s, 0)
        self.assertEqual(before,git(self.repo,'worktree','list','--porcelain'))
        pid = int((self.root/'pid').read_text())
        status = Path('/proc')/str(pid)/'stat'
        until=time.monotonic()+1
        while status.exists() and status.read_text().split()[2]!='Z' and time.monotonic()<until:
            time.sleep(.01)
        self.assertTrue(not status.exists() or status.read_text().split()[2] == 'Z')
        for budget in (0, -1, 181, float('nan'), float('inf'), 'bad'):
            self.assertTrue(self.run_entries([e], budget_s=budget).undetermined)
        with patch.object(sweep, 'static', side_effect=RuntimeError('fixture exception')):
            result = self.run_entries([e])
        self.assertTrue(result.undetermined)
        self.assertFalse(list(self.root.glob('preflight-*')))
        # Boundary expiry is checked even when no command needs a timeout.
        with patch.object(sweep.time, 'monotonic', return_value=4):
            with self.assertRaises(TimeoutError):
                sweep.remaining(3)
        with patch.object(sweep,'python_for',return_value='/no/such/python'):
            result=self.run_entries([e])
        self.assertTrue(result.undetermined)
        response('AC5', dict(wall_s=result.wall_s,cpu_s=result.cpu_s,group_stopped=True,cleanup=True))

    def cli(self, module, args, project=PROJECT, **extra):
        env=dict(os.environ, DOIT_ROOT=str(self.state), DOIT_LEDGER_FILE='L-builder-cli.jsonl')
        env.update(extra)
        return subprocess.run([sys.executable,str(HERE/'src'/module),'--project',project,'--on',self.base,'--specs',args],
                              env=env,capture_output=True,text=True,timeout=45)

    def test_ac5_clock_boundary_stops_static_and_later_work(self):
        e=self.entry('boundary',{'outside.txt':'new\n'})
        steps=stacker.stack(self.repo,self.base,[e])
        clock=[1]
        messages=[]
        def stack(*args):
            clock[0]=4
            return steps
        channel=SimpleNamespace(send=messages.append,close=lambda:None)
        with patch.object(sweep.os,'setsid'), patch.object(sweep.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(stacker,'stack',side_effect=stack),patch.object(sweep,'static') as checks,patch.object(sweep,'command') as commands:
            sweep._worker(self.repo,self.base,[e[0]],None,self.root,3,channel)
        result=messages[-1][1]
        self.assertTrue(result.undetermined)
        self.assertEqual(result.stage,'static')
        self.assertEqual(result.trace,['stack','static'])
        checks.assert_not_called()
        commands.assert_not_called()

    def seed_all_guards(self):
        # Real CLI loads only the shipped inventory, never a fixture override.
        grouped={}
        os.environ.pop('DOIT_PREFLIGHT_GUARDS', None)
        for node in sweep.guard_inventory(HERE/'lane/guard-tests.txt')[0]:
            path,*names=node.split('::')
            grouped.setdefault(path,[]).append(names)
        for path, names in grouped.items():
            body=''
            for index, parts in enumerate(names):
                if len(parts)==2:
                    body+='class '+parts[0]+':\n    def '+parts[1]+'(self): assert True\n'
                else:
                    body+='def '+(parts[0] if parts else 'test_fixture_'+str(index))+'(): assert True\n'
            self.fx.write(path,body)
        self.base=self.fx.commit('all guards')
        self.fx.base=self.base

    def test_ac6_cli_statuses_tree_and_checkout_preserved(self):
        self.seed_all_guards()
        e = self.entry('cli-ok', {'outside.txt':'new\n'})
        before=(git(self.repo,'show-ref'),git(self.repo,'status','--porcelain'),git(self.repo,'rev-parse','HEAD'))
        p=self.cli('preflight_sweep.py',e[0])
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertIn('PREFLIGHT-CLEAN',p.stdout)
        command=[sys.executable,str(HERE/'src/preflight_sweep.py'),'--project',PROJECT,'--on',self.base,'--specs',e[0],'--tree']
        for tree,code in [(e[2],0),(self.base,2),('missing',2)]:
            p=subprocess.run(command+[tree],env=os.environ,capture_output=True,text=True,timeout=45)
            self.assertEqual(p.returncode,code,p.stdout+p.stderr)
        a=self.entry('cli-a',{'outside.txt':'a\n'})
        b=self.entry('cli-b',{'outside.txt':'b\n'})
        p=self.cli('rechain_check.py',a[0]+','+b[0])
        self.assertEqual(p.returncode,1,p.stdout+p.stderr)
        self.assertEqual(len(p.stdout.splitlines()),2)
        self.assertIn('content-conflict',p.stdout)
        p=self.cli('preflight_sweep.py',a[0]+','+b[0])
        self.assertEqual(p.returncode,1,p.stdout+p.stderr)
        self.assertIn('PREFLIGHT-RED stack cli-b content-conflict',p.stdout)
        for ids in ('missing','../bad','$(touch pwned)'):
            self.assertEqual(self.cli('preflight_sweep.py',ids).returncode,2)
        # Compare the state after fixtures were created; sweep must not move it.
        state=(git(self.repo,'show-ref'),git(self.repo,'status','--porcelain'),git(self.repo,'rev-parse','HEAD'))
        self.assertEqual(self.cli('rechain_check.py',e[0]).returncode,0)
        self.assertEqual(state,(git(self.repo,'show-ref'),git(self.repo,'status','--porcelain'),git(self.repo,'rev-parse','HEAD')))
        self.assertEqual(before[1:],state[1:])
        response('AC6',dict(exits=[0,1,2],tree_mismatch_held=True,checkout_unchanged=True))

    def test_ac6_every_red_class_and_rechain_diagnostics(self):
        self.seed_all_guards()
        cases=[({'outside.txt':'bad\n'},['other.txt'],'writes'),
               ({'tests/test_bad_marker.py':'# ScratchRequired\ndef test_ok(): pass\n'},None,'pg-marker'),
               ({rechain.VERSIONS+'bad.py':"revision='bad'\ndown_revision='absent'\n"},None,'alembic-heads'),
               ({'tests/test_guard.py':'def test_guard(): assert False\n'},None,'guard'),
               ({'tests/test_failing.py':'def test_failing(): assert False\n'},None,'changed-test')]
        # This fixture guard is additional to the shipped set, so mutate one of
        # the actual shipped files for the guard-red case.
        guard=sweep.guard_inventory(HERE/'lane/guard-tests.txt')[0][0]
        for index,(changes,grant,cls) in enumerate(cases):
            if cls=='guard':
                changes={guard:'def test_fixture_0(): assert False\n'}
            entry=self.entry('red'+str(index),changes,grant)
            p=self.cli('preflight_sweep.py',entry[0])
            self.assertEqual(p.returncode,1,p.stdout+p.stderr)
            self.assertIn(' '+cls+' ',p.stdout)
            print('AC6',p.stdout.strip())
        a=self.entry('overlap-a',{'app/a.py':'a=1\n'},['app/'])
        b=self.entry('overlap-b',{'app/b.py':'b=1\n'},['app/'])
        p=self.cli('preflight_sweep.py',a[0]+','+b[0])
        self.assertEqual(p.returncode,1,p.stdout+p.stderr)
        self.assertIn(' overlap ',p.stdout)
        a,b=self.fx.branch('a'),self.fx.branch('b')
        for spec,branch,sha in (a,b):
            self.register(spec,branch,git(self.repo,'diff','--name-only',self.base,sha).splitlines())
        p=self.cli('preflight_sweep.py','a,b')
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertIn('PREFLIGHT-RECHAINED b h0 a',p.stdout)
        c=self.fx.branch('c',mutation=lambda f:f.write(rechain.VERSIONS+'extra.py',"revision='extra'\ndown_revision='c'\n"))
        self.register(c[0],c[1],git(self.repo,'diff','--name-only',self.base,c[2]).splitlines())
        p=self.cli('preflight_sweep.py','a,c')
        self.assertEqual(p.returncode,1,p.stdout+p.stderr)
        self.assertIn(' declined ',p.stdout)
        self.assertFalse((self.repo/'pwned').exists())

    def test_ac7_production_append_fresh_fold_and_rejection(self):
        e=self.entry('ledger',{'outside.txt':'ledger\n'})
        red=self.entry('ledger-red',{'outside.txt':'red\n'},['wrong.txt'])
        for specs,budget in [([e[0]],180),([e[0]],0),(['missing'],180),([red[0]],180)]:
            sweep.run(self.repo,self.base,specs,budget_s=budget)
        script='import fold,json; e=fold.read_events(use_cache=False); s,c,i,b=fold.fold(e); print(json.dumps({"events":[x for x in e if x["type"]=="preflight-swept"],"ignored":i,"subjects":list(b)}))'
        p=subprocess.run([sys.executable,'-c',script],cwd=HERE,env=dict(os.environ,PYTHONPATH=str(HERE/'src')),capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
        data=json.loads(p.stdout)
        rows=data['events']
        self.assertEqual([r['subject'] for r in rows],['ledger','ledger','missing','ledger-red'])
        self.assertEqual([r['ok'] for r in rows],[True,False,False,False])
        self.assertEqual(rows[-1]['class'],'writes')
        self.assertFalse(rows[-1]['undetermined'])
        self.assertFalse(data['ignored'])
        for row in rows:
            for key in ('batch','base','ok','stage','class','failed','cpu_s','wall_s','undetermined'):
                self.assertIn(key,row)
            self.assertIsInstance(row['cpu_s'],(float,int))
            self.assertIsInstance(row['wall_s'],(float,int))
        self.assertIn('ledger',data['subjects'])
        with patch.dict(os.environ,DOIT_LEDGER_FILE='L-grader-fixture.jsonl'):
            result=self.run_entries([e])
            self.assertTrue(result.undetermined)
            self.assertFalse(result.ok)
        with patch.object(fold,'append',side_effect=OSError('fixture')):
            self.assertTrue(self.run_entries([e]).undetermined)
        self.assertEqual(self.cli('preflight_sweep.py',e[0],DOIT_LEDGER_FILE='L-grader-denied.jsonl').returncode,2)
        response('AC7',dict(subjects=[r['subject'] for r in rows],ok=[r['ok'] for r in rows],fresh_fold=True,unauthorized_held=True))

    def folded(self):
        script='import fold,json; e=fold.read_events(use_cache=False); s,c,i,b=fold.fold(e); print(json.dumps({"events":e,"ignored":i}))'
        p=subprocess.run([sys.executable,'-c',script],cwd=HERE,env=dict(os.environ,PYTHONPATH=str(HERE/'src')),capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
        return json.loads(p.stdout)

    def test_ac7_four_actors_cli_thinker_and_no_speculative_events(self):
        e=self.entry('actor',{'outside.txt':'actor\n'})
        for actor in ('executor','operator','builder','thinker'):
            with patch.dict(os.environ,DOIT_LEDGER_FILE='L-'+actor+'-fixture.jsonl'):
                result=self.run_entries([e])
            self.assertTrue(result.ok,(actor,result.findings))
        p=self.cli('preflight_sweep.py',e[0],DOIT_LEDGER_FILE='L-thinker-cli.jsonl',DOIT_ACTOR='thinker')
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        # A rechained success and a declined shape leave only preflight-swept verdicts.
        a,b=self.fx.branch('ra'),self.fx.branch('rb')
        for spec,branch,sha in (a,b):
            self.register(spec,branch,git(self.repo,'diff','--name-only',self.base,sha).splitlines())
        self.assertEqual(self.cli('preflight_sweep.py','ra,rb',DOIT_ACTOR='thinker',DOIT_LEDGER_FILE='L-thinker-cli.jsonl').returncode,0)
        c=self.fx.branch('rc',mutation=lambda f:f.write(rechain.VERSIONS+'extra.py',"revision='extra'\ndown_revision='rc'\n"))
        self.register(c[0],c[1],git(self.repo,'diff','--name-only',self.base,c[2]).splitlines())
        self.assertEqual(self.cli('preflight_sweep.py','ra,rc',DOIT_LEDGER_FILE='L-thinker-cli.jsonl').returncode,1)
        data=self.folded()
        self.assertFalse(data['ignored'])
        rows=[x for x in data['events'] if x['type']=='preflight-swept']
        self.assertEqual({r['actor'] for r in rows if r['subject']=='actor'},{'executor','operator','builder','thinker'})
        self.assertEqual([r['subject'] for r in rows if r['actor']=='thinker'][-5:],['actor','ra','rb','ra','rc'])
        self.assertTrue(all(isinstance(r['wall_s'],(int,float)) and isinstance(r['cpu_s'],(int,float)) for r in rows))
        self.assertFalse([x for x in data['events'] if x['type'] in ('rechained','rechain-declined')])
        for denied in ('grader','reviewer','planner'):
            with patch.dict(os.environ,DOIT_LEDGER_FILE='L-'+denied+'-fixture.jsonl'):
                self.assertTrue(self.run_entries([e]).undetermined,denied)


if __name__=='__main__':
    unittest.main(verbosity=2)
