#!/usr/bin/env python3
"""R8: fixture cgroups, fake Docker, synthetic JUnit; never change a runner."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

CI = Path(__file__).resolve().parents[1] / 'scripts/ci'


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), CI / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cpu, job, evidence = (load(name) for name in ('cpu-budget', 'isolated-job', 'runner-evidence'))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='runner-parity-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / 'proc'
        (self.proc / 'self').mkdir(parents=True)
        self.cg = self.root / 'cgroup'
        (self.cg / 'parent/job').mkdir(parents=True)
        (self.proc / 'self/cgroup').write_text('0::/parent/job\n')
        (self.cg / 'cpu.max').write_text('max 100000\n')
        (self.cg / 'parent/cpu.max').write_text('400000 100000\n')
        (self.cg / 'parent/job/cpu.max').write_text('600000 100000\n')

    def test_quota_ancestors_affinity_fraction_and_unlimited(self):
        self.assertEqual(cpu.workers(self.proc, self.cg, 8), 4)
        self.assertEqual(cpu.workers(self.proc, self.cg, 2), 2)
        (self.cg / 'parent/job/cpu.max').write_text('350000 100000')
        self.assertEqual(cpu.workers(self.proc, self.cg, 8), 3)
        (self.cg / 'parent/job/cpu.max').write_text('50000 100000')
        self.assertEqual(cpu.workers(self.proc, self.cg, 8), 1)
        for file in self.cg.rglob('cpu.max'):
            file.write_text('max 100000')
        self.assertEqual(cpu.workers(self.proc, self.cg, 8), 8)

    def test_cgroup_v1_and_mount_subtree(self):
        (self.proc / 'self/cgroup').write_text('2:cpu,cpuacct:/job\n')
        child = self.cg / 'cpu/job'
        child.mkdir(parents=True)
        (child / 'cpu.cfs_quota_us').write_text('250000')
        (child / 'cpu.cfs_period_us').write_text('100000')
        self.assertEqual(cpu.workers(self.proc, self.cg, 8), 2)
        (self.proc / 'self/cgroup').write_text('0::/service/job\n')
        (self.proc / 'self/mountinfo').write_text(f'1 2 0:1 /service {self.cg} rw - cgroup2 cgroup rw\n')
        (self.cg / 'job').mkdir()
        (self.cg / 'job/cpu.max').write_text('300000 100000')
        self.assertEqual(cpu.workers(self.proc, self.cg, 8), 3)

    def test_invalid_quota_fails(self):
        for text in ('0 100000', '400000 0', 'broken'):
            (self.cg / 'parent/job/cpu.max').write_text(text)
            with self.assertRaises(ValueError):
                cpu.workers(self.proc, self.cg, 8)

    def test_private_job_plan_and_cleanup(self):
        image = 'ci@sha256:' + 'a' * 64
        commands = job.plan(self.root, image, ['echo', 'argument with spaces'], 4, 1000, 1000, 'ci-fixture', 'net:[123]')
        argv = commands[1]
        self.assertIn('--network', argv)
        self.assertIn('ci-fixture', argv)
        self.assertIn('--cpus', argv)
        self.assertIn('4', argv)
        self.assertNotIn('--privileged', argv)
        self.assertNotIn('host', argv)
        self.assertNotIn('/var/run/docker.sock', ' '.join(argv))
        self.assertIn('CI_HOST_NETNS=net:[123]', argv)
        self.assertEqual(argv[-2:], ['echo', 'argument with spaces'])
        calls = []
        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 9 if argv == commands[1] else 0)
        self.assertEqual(job.execute(commands, run=run), 9)
        self.assertEqual(calls, commands)
        with self.assertRaises(ValueError):
            job.plan(self.root, 'ci:latest', ['true'], 4, 1, 1, 'ci', 'net')
        def crash(argv, **kw):
            calls.append(argv)
            if argv == commands[1]:
                raise OSError('launch failed')
            return subprocess.CompletedProcess(argv, 0)
        with self.assertRaises(OSError):
            job.execute(commands, run=crash)
        self.assertEqual(calls[-1], commands[-1])

    def test_default_cli_only_prints_plan(self):
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        docker = bin_dir / 'docker'
        marker = self.root / 'docker-called'
        docker.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 99\n')
        docker.chmod(0o755)
        result = subprocess.run([sys.executable, str(CI / 'isolated-job.py'),
            '--checkout', str(self.root), '--image', 'ci@sha256:' + 'a' * 64, '--', 'true'],
            capture_output=True, text=True, env=dict(os.environ, PATH=str(bin_dir) + ':/usr/bin:/bin'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)), 4)
        self.assertFalse(marker.exists())

    def test_namespace_gate_prevents_host_probe(self):
        with patch.dict(os.environ, {'CI_HOST_NETNS': 'net:[123]'}), \
                patch.object(evidence.os, 'readlink', return_value='net:[123]'), \
                patch.object(evidence.socket, 'socket', side_effect=AssertionError('no host probe')):
            with self.assertRaises(ValueError):
                evidence.probe_namespace()

    def test_worker_wrapper_passes_quota_to_pytest(self):
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'python3').write_text('#!/bin/sh\necho 3\n')
        project = bin_dir / 'project-python'
        project.write_text('#!/bin/sh\nprintf "%s\\n" "$CI_WORKERS" "$PYTEST_XDIST_AUTO_NUM_WORKERS" "$@"\n')
        for path in bin_dir.iterdir():
            path.chmod(0o755)
        result = subprocess.run(['bash', str(CI / 'run-pytest.sh'), 'test file.py'], capture_output=True, text=True,
            env=dict(os.environ, PATH=str(bin_dir) + ':/usr/bin:/bin', CI_PROJECT_PYTHON=str(project)))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), ['3', '3', '-m', 'pytest', '-n', '3', 'test file.py'])

    def test_evidence_parity_detects_failures_skips_and_collection(self):
        junit = self.root / 'junit.xml'
        junit.write_text('<testsuite><testcase classname="c" name="a"/>'
                         '<testcase classname="c" name="b"><skipped/></testcase></testsuite>')
        tests = evidence.results(junit)
        self.assertEqual(tests, {'c::a': 'passed', 'c::b': 'skipped'})
        hosted = dict(sha='abc', tests=tests, toolchain={'python': '3.12'}, wall_seconds=100)
        candidate = dict(hosted, private_netns=True)
        self.assertEqual(evidence.compare(hosted, candidate, 4200), [])
        for field, value, message in [('sha', 'def', 'sha differs'), ('toolchain', {}, 'toolchain differs'),
                                      ('tests', {'c::a': 'passed'}, 'tests differs'),
                                      ('wall_seconds', 4200, 'candidate exceeds window budget'),
                                      ('private_netns', False, 'candidate isolation not proven')]:
            self.assertIn(message, evidence.compare(hosted, dict(candidate, **{field: value}), 4200))
        bad = dict(candidate, tests={'c::a': 'failed'})
        self.assertIn('candidate has failures', evidence.compare(hosted, bad, 4200))
        for wall in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                evidence.compare(hosted, dict(candidate, wall_seconds=wall), 4200)
        junit.write_text('<testsuite/>')
        with self.assertRaises(ValueError):
            evidence.results(junit)


if __name__ == '__main__':
    unittest.main()
