"""Tip/base evidence survives added tests and pre-existing red chain steps."""
import json
import pathlib
import subprocess
import sys
import unittest

import test_build_runner as fixtures


class BaseComparisonTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.BuildRunnerTests('test_env_none_row')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.python = sys.executable
        self.pytest = next((p for p in (sys.executable,
            '/opt/albert-scott/.venv/bin/python', '/home/albert/do-it-v2/.venv/bin/python')
            if pathlib.Path(p).is_file() and subprocess.run(
                [p, '-B', '-c', 'import pytest'], capture_output=True).returncode == 0), None)
        self.assertIsNotNone(self.pytest, 'a local pytest interpreter is required')
        self.f.env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'

    def commit_base(self):
        self.f.base = self.f.commit('base tests')

    def tip(self, command):
        self.f.sha = self.f.commit('tip tests')
        self.f.build_done()
        if '\n' in command:
            self.f.spec(verification=command)
        else:
            self.f.spec(command)
        return self.f.bundle()[0]

    def test_mixed_pytest_files_and_absent_node(self):
        old = self.f.repo / 'test_old.py'
        old.write_text('def test_red(): assert False\ndef test_green(): pass\n')
        self.commit_base()
        old.write_text(old.read_text() + 'def test_added(): assert False\n')
        (self.f.repo / 'test_new.py').write_text('def test_new(): assert False\n')
        data = self.tip(f'{self.pytest} -m pytest -q test_old.py::test_red '
                        'test_old.py::test_added test_new.py')
        self.assertEqual(data['comparison'], {
            'pre_existing': ['test_old.py::test_red'],
            'new': ['test_new.py::test_new', 'test_old.py::test_added'],
            'undetermined': []})
        self.assertNotEqual(data['base'][0]['exit'], 4)
        self.f.assert_clean()

    def test_chain_runs_later_acs_and_preserves_shell_setup(self):
        sub = self.f.repo / 'sub'
        sub.mkdir()
        (sub / 'old.py').write_text("print('NEW failed: old.py::red'); raise SystemExit(1)\n")
        (sub / 'last.py').write_text("import os; assert os.environ['CHAIN_VALUE']=='a && b'; print('LAST_AC_RAN')\n")
        self.commit_base()
        (sub / 'new.py').write_text("print('NEW failed: new.py::red'); raise SystemExit(1)\n")
        command = ("cd sub && export CHAIN_VALUE='a && b' && set -e &&\n"
                   f'# AC1\n{self.python} old.py &&\n'
                   f'# AC2\n{self.python} new.py &&\n'
                   f'# AC3\n{self.python} last.py')
        data = self.tip(command)
        self.assertEqual(data['comparison'], {'pre_existing': ['old.py::red'],
                                             'new': ['new.py::red'], 'undetermined': []})
        self.assertIn(b'LAST_AC_RAN', self.f.tail(data['commands'][0]))
        self.assertIn(b'LAST_AC_RAN', self.f.tail(data['base'][0]))

    def test_missing_unittest_module_and_native_failure_ids(self):
        (self.f.repo / 'test_old.py').write_text(
            'import unittest\nclass Old(unittest.TestCase):\n'
            ' def test_old(self): self.fail("known")\n')
        self.commit_base()
        (self.f.repo / 'test_new.py').write_text(
            'import unittest\nclass Added(unittest.TestCase):\n'
            ' def test_new(self): self.fail("new")\n')
        data = self.tip(f'{self.python} -m unittest test_old test_new')
        self.assertEqual(data['comparison'], {
            'pre_existing': ['test_old.py::Old::test_old'],
            'new': ['test_new.py::Added::test_new'], 'undetermined': []})

    def test_collection_errors_do_not_turn_existing_tests_into_new_failures(self):
        old = self.f.repo / 'test_old.py'
        old.write_text('import missing_fixture_dependency\ndef test_old(): assert False\n')
        self.commit_base()
        old.write_text('def test_old(): assert False\n')
        data = self.tip(f'{self.pytest} -m pytest -q test_old.py')
        self.assertEqual(data['comparison'], {'pre_existing': [], 'new': [], 'undetermined': [0]})

    def test_absent_tests_still_classified_when_another_base_file_cannot_import(self):
        old = self.f.repo / 'test_old.py'
        old.write_text('import missing_fixture_dependency\ndef test_old(): assert False\n')
        self.commit_base()
        old.write_text('def test_old(): assert False\n')
        (self.f.repo / 'test_new.py').write_text('def test_new(): assert False\n')
        data = self.tip(f'{self.pytest} -m pytest -q test_old.py test_new.py')
        self.assertEqual(data['comparison'], {'pre_existing': [],
            'new': ['test_new.py::test_new'], 'undetermined': [0]})

    def test_failfast_base_does_not_establish_that_unexecuted_tests_passed(self):
        (self.f.repo / 'test_old.py').write_text(
            'def test_first(): assert False\ndef test_later(): assert False\n')
        self.commit_base()
        (self.f.repo / 'test_old.py').write_text(
            'def test_first(): pass\ndef test_later(): assert False\n')
        data = self.tip(f'{self.pytest} -m pytest -x -q test_old.py')
        self.assertEqual(data['comparison'], {'pre_existing': [], 'new': [], 'undetermined': [0]})

    def test_nested_chain_checks_run_without_leaking_subshell_cwd(self):
        sub = self.f.repo / 'sub'
        sub.mkdir()
        (sub / 'old.py').write_text("print('NEW failed: old.py::red'); raise SystemExit(1)\n")
        (self.f.repo / 'last.py').write_text("import pathlib; assert pathlib.Path('sub').is_dir(); print('LAST_AC_RAN')\n")
        self.commit_base()
        (sub / 'new.py').write_text("print('NEW failed: new.py::red'); raise SystemExit(1)\n")
        data = self.tip(f'(cd sub && {self.python} old.py && {self.python} new.py) &&\n'
                        f'{self.python} last.py')
        self.assertEqual(data['comparison'], {'pre_existing': ['old.py::red'],
                                             'new': ['new.py::red'], 'undetermined': []})
        for row in data['commands'] + data['base']:
            self.assertIn(b'LAST_AC_RAN', self.f.tail(row))

    def test_runner_revision_regenerates_legacy_cached_evidence(self):
        data, path = self.f.bundle()
        legacy = path.with_name(path.name.replace('-r2.json', '.json'))
        data['runner_version'] = '1'
        data['comparison']['undetermined'] = [0]
        if legacy != path:
            path.with_suffix('').rename(legacy.with_suffix(''))
            for row in data['commands'] + data['base']:
                row['tail'] = str(legacy.with_suffix('') / pathlib.Path(row['tail']).name)
            path.unlink()
        legacy.write_text(json.dumps(data))
        refreshed, new_path = self.f.bundle()
        self.assertEqual(refreshed['runner_version'], '2')
        self.assertNotEqual(new_path, legacy)
        self.assertEqual(refreshed['comparison']['undetermined'], [])

    def test_only_added_selections_are_skipped_cleanly_at_base(self):
        old = self.f.repo / 'test_old.py'
        old.write_text('def test_green(): pass\n')
        self.commit_base()
        old.write_text(old.read_text() + 'def test_added(): assert False\n')
        new = self.f.repo / 'new_directory' / 'test_new.py'
        new.parent.mkdir()
        new.write_text('def test_new(): assert False\n')
        data = self.tip(f'{self.pytest} -m pytest -q test_old.py::test_added new_directory/test_new.py')
        self.assertEqual(data['base'][0]['exit'], 0)
        self.assertEqual(data['comparison'], {'pre_existing': [],
            'new': ['new_directory/test_new.py::test_new', 'test_old.py::test_added'],
            'undetermined': []})

    def test_python_wrapper_can_reset_project_pythonpath(self):
        (self.f.repo / 'test_old.py').write_text('def test_old(): assert False\n')
        self.commit_base()
        (self.f.repo / 'test_new.py').write_text('def test_new(): assert False\n')
        code = ("import os,subprocess,sys; os.environ['PYTHONPATH']='.'; "
                "assert subprocess.run(['git','check-ignore','-q',os.environ['PYTEST_PLUGINS']+'.py']).returncode==0; "
                "sys.exit(subprocess.run([sys.executable,'-m','pytest','-q','test_old.py','test_new.py']).returncode)")
        import shlex
        data = self.tip(f'{self.pytest} -c ' + shlex.quote(code))
        self.assertEqual(data['comparison'], {'pre_existing': ['test_old.py::test_old'],
                                             'new': ['test_new.py::test_new'], 'undetermined': []})

    def test_directory_selection_keeps_existing_tests_beside_a_new_selector(self):
        old = self.f.repo / 'test_old.py'
        old.write_text('def test_old(): assert False\n')
        self.commit_base()
        old.write_text(old.read_text() + 'def test_added(): assert False\n')
        data = self.tip(f'{self.pytest} -m pytest -q . test_old.py::test_added')
        self.assertEqual(data['comparison'], {'pre_existing': ['test_old.py::test_old'],
                                             'new': ['test_old.py::test_added'], 'undetermined': []})

    def test_repeated_pytest_sessions_do_not_reselect_previous_targets(self):
        old = self.f.repo / 'test_old.py'
        source = ('runs=0\ndef test_once():\n global runs\n runs+=1\n assert runs==1\n'
                  'def test_changed(): pass\n')
        old.write_text(source)
        self.commit_base()
        old.write_text(source.replace('def test_changed(): pass', 'def test_changed(): assert False'))
        import shlex
        code = ("import pytest,sys; a=pytest.main(['-q','test_old.py::test_once']); "
                "b=pytest.main(['-q','test_old.py::test_changed']); sys.exit(max(a,b))")
        data = self.tip(f'{self.pytest} -c ' + shlex.quote(code))
        self.assertEqual(data['base'][0]['exit'], 0)
        self.assertEqual(data['comparison'], {'pre_existing': [],
                                             'new': ['test_old.py::test_changed'], 'undetermined': []})

    def test_direct_unittest_scripts_with_interpreter_options(self):
        source = ('import unittest\nclass Cases(unittest.TestCase):\n'
                  ' def test_red(self): self.fail("fixture")\n'
                  'if __name__=="__main__": unittest.main()\n')
        (self.f.repo / 'test_old.py').write_text(source)
        self.commit_base()
        (self.f.repo / 'test_new.py').write_text(source)
        data = self.tip(f'{self.python} -X dev test_old.py &&\n{self.python} -X dev test_new.py')
        self.assertEqual(data['comparison'], {'pre_existing': ['test_old.py::Cases::test_red'],
            'new': ['test_new.py::Cases::test_red'], 'undetermined': []})

    def test_new_shell_script_keeps_existing_missing_file_classification(self):
        (self.f.repo / 'new.sh').write_text('echo "NEW failed: new.sh::red"\nexit 1\n')
        data = self.tip('bash new.sh')
        self.assertEqual(data['comparison'], {'pre_existing': [], 'new': ['new.sh::red'],
                                             'undetermined': []})

    def test_added_node_selected_by_keyword_is_new_after_clean_base_collection(self):
        old = self.f.repo / 'test_old.py'
        old.write_text('def test_old(): pass\n')
        self.commit_base()
        old.write_text(old.read_text() + 'def test_added_ac8(): assert False\n')
        data = self.tip(f'{self.pytest} -m pytest -q test_old.py -k added_ac8')
        self.assertEqual(data['comparison'], {'pre_existing': [],
                                             'new': ['test_old.py::test_added_ac8'], 'undetermined': []})

    def test_base_deselection_does_not_make_present_node_new(self):
        (self.f.repo / 'test_old.py').write_text('def test_old(): assert False\n')
        config = self.f.repo / 'conftest.py'
        config.write_text('import pytest\n@pytest.hookimpl(tryfirst=True)\n'
                          'def pytest_collection_modifyitems(items): items.clear()\n')
        self.commit_base()
        config.write_text('')
        data = self.tip(f'{self.pytest} -m pytest -q test_old.py')
        self.assertEqual(data['comparison'], {'pre_existing': [], 'new': [], 'undetermined': [0]})

    def test_parallel_pytest_uses_worker_node_evidence(self):
        if subprocess.run([self.pytest, '-B', '-c', 'import xdist'], capture_output=True).returncode:
            self.skipTest('pytest-xdist is not installed')
        (self.f.repo / 'test_old.py').write_text('def test_old(): assert False\n')
        self.commit_base()
        (self.f.repo / 'test_new.py').write_text('def test_new(): assert False\n')
        data = self.tip(f'{self.pytest} -m pytest -p xdist.plugin -n 2 -q test_old.py test_new.py')
        self.assertEqual(data['commands'][0]['exit'], 1)
        self.assertEqual(data['base'][0]['exit'], 1)
        self.assertEqual(data['comparison'], {'pre_existing': ['test_old.py::test_old'],
                                             'new': ['test_new.py::test_new'], 'undetermined': []})

    def test_native_pytest_internal_error_is_not_complete_evidence(self):
        old = self.f.repo / 'test_old.py'
        old.write_text('def test_old(): pass\n')
        config = self.f.repo / 'conftest.py'
        config.write_text('')
        self.commit_base()
        old.write_text('def test_old(): assert False\n')
        config.write_text('import pytest\n@pytest.hookimpl(trylast=True)\n'
            'def pytest_runtest_logreport(report):\n'
            ' if report.when=="call": raise RuntimeError("fixture internal error")\n')
        data = self.tip(f'{self.pytest} -m pytest -q test_old.py')
        self.assertEqual(data['commands'][0]['exit'], 3)
        self.assertEqual(data['comparison']['undetermined'], [0])


class ShellProcessTests(unittest.TestCase):
    def test_background_writer_remains_subject_to_the_process_deadline(self):
        import contextlib
        import os
        import shlex
        import signal
        import tempfile
        import time
        import runner_evidence
        import build_runner
        with tempfile.TemporaryDirectory(prefix='runner-chain-process-') as name:
            root = pathlib.Path(name)
            marker = root / 'writer.pid'
            child = ("import pathlib,subprocess,sys; p=subprocess.Popen([sys.executable,'-c',"
                     "'import time; time.sleep(60)',sys.argv[1]]); pathlib.Path(sys.argv[1]).write_text(str(p.pid))")
            command = shlex.join([sys.executable, '-c', child, str(marker)]) + ' && printf done'
            script, _ = runner_evidence.shell(command, root, False)
            try:
                with self.assertRaises(build_runner.WallCap):
                    build_runner._process(['/bin/bash', '-c', script], env=dict(os.environ),
                                          cwd=root, deadline=time.monotonic() + 2)
                self.assertTrue(marker.exists(), 'the background writer must actually start')
            finally:
                if marker.exists():
                    pid = int(marker.read_text())
                    with contextlib.suppress(ProcessLookupError, FileNotFoundError):
                        if str(marker).encode() in pathlib.Path(f'/proc/{pid}/cmdline').read_bytes():
                            os.kill(pid, signal.SIGKILL)


if __name__ == '__main__':
    unittest.main()
