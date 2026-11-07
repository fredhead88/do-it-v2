"""Real scanner/reader regressions for automatic quarantine policy evidence."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import quarantine_file as policy
import rechain


PROJECT = 'albert-scott'
NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
DEPENDENCIES = Path('/opt/albert-scott')
SCANNER = 'scripts/ci/detect-secrets-changed.sh'
READER = 'scripts/ci/pytest_baseline_gate.py'
DETECT_SECRETS = DEPENDENCIES / '.venv/bin/detect-secrets'


class QuarantineSecretScanTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='quarantine-secret-scan-')
        self.addCleanup(self.scratch.cleanup)
        self.repo = Path(self.scratch.name) / 'repo'
        self.repo.mkdir()
        self.dependencies = {}
        for name in (SCANNER, READER, '.secrets.baseline'):
            source = DEPENDENCIES / name
            self.assertTrue(source.is_file(), 'required real scanner/reader dependency missing')
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            self.dependencies[name] = source.read_bytes()
        self.assertTrue(os.access(DETECT_SECRETS, os.X_OK), 'detect-secrets is required')
        (self.repo / policy.PATH).write_text('# quarantine\n')
        self.git('init', '-q', '-b', 'fixture-main')
        self.git('add', '.')
        self.base = self.commit('fixture base')
        # Varied, reproducible Git-shaped identifiers; never generated credentials.
        shas = [hashlib.sha1(('rehearsal-' + str(i)).encode()).hexdigest() for i in range(10)]
        tree = lambda subject, sha, obj: 'tree:' + json.dumps([subject, sha, obj], separators=(',', ':'))
        nightly = 'nightly:' + json.dumps(['L-nightly-local.jsonl:31', shas[4]], separators=(',', ':'))
        sources = [
            ('repeat-blame', ['drop-w1', 'drop-w2', 'drop-' + shas[0]]),
            ('auto-base-red', ['ci:run:42350548702:1', 'ci:https://ci.invalid/runs/42350548703?sha=' + shas[1]]),
            ('auto-base-red', ['window:w-' + shas[2], 'window:w-second']),
            ('two-specs', [tree('w-shared', shas[0], shas[2]), tree('w-shared', shas[1], shas[3])]),
            ('master-seed', [nightly]),
            ('auto-base-red', ['ruling:L-operator-0001.jsonl:42', 'ruling:' + shas[5] + ':7']),
            ('two-specs', [tree('legacy-window', shas[6], shas[7])]),
            ('two-specs', [tree('short-window', 'abc1234', 'def5678')]),
        ]
        self.events = [dict(type='test-quarantined', actor='base-red', project=PROJECT,
                            node='tests/test_fixture.py::test_' + str(i), rule=rule,
                            windows=windows, ts=NOW.isoformat(), since=NOW.isoformat(),
                            until=(NOW + timedelta(days=7)).isoformat(), owner='auto-2026-11-07')
                       for i, (rule, windows) in enumerate(sources)]
        spec = importlib.util.spec_from_file_location('quarantine_fixture_reader', self.repo / READER)
        self.reader = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.reader
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(self.reader)

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, 'fixture Git operation failed')
        return result.stdout.decode().strip()

    def commit(self, message):
        self.git(*rechain.IDENT, 'commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def write_commit(self, content):
        (self.repo / policy.PATH).write_text(content)
        self.git('add', policy.PATH)
        return self.commit('fixture policy')

    def scan(self, head, expected):
        paths = self.git('diff', '--name-only', self.base + '...' + head).splitlines()
        self.assertEqual(paths, [policy.PATH], 'the policy must be the scanned changed path')
        env = dict(os.environ, DETECT_SECRETS_BIN=str(DETECT_SECRETS))
        result = subprocess.run(['/usr/bin/bash', str(self.repo / SCANNER), 'scan', self.base,
                                 head, '.secrets.baseline'], cwd=self.repo, env=env,
                                capture_output=True, text=True, timeout=120)
        # Retain types/counts and statuses only, never control values or raw diagnostics.
        types = re.findall(r' -- ([^\r\n]+)', result.stdout)
        summary = dict(exit_code=result.returncode, finding_types=sorted(set(types)),
                       findings=len(types), changed_paths=paths,
                       negative_control_passed='Checking 1 relevant changed file(s); scanning 1 source file(s)' in result.stdout
                       and ('potential secret(s)' in result.stdout or 'No secrets detected' in result.stdout))
        self.assertEqual(result.returncode, expected, json.dumps(summary))
        self.assertTrue(summary['negative_control_passed'], json.dumps(summary))
        for name, content in self.dependencies.items():
            self.assertEqual((self.repo / name).read_bytes(), content, 'security dependency changed')
            self.assertEqual((DEPENDENCIES / name).read_bytes(), content, 'source dependency changed')
        return summary

    def proof(self, **body):
        encoded = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url=Path(__file__).resolve().as_uri() + '#' + self._testMethodName,
                              status=0, body_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                              body_excerpt=body), sort_keys=True))

    def test_ac1_secret_scan(self):
        # This is the old four-field fixture, intentionally retaining full source SHAs.
        old = '# quarantine\n' + ''.join('\t'.join((event['node'], event['owner'], '2026-11-14',
                                       event['rule'] + ' evidence=' + ','.join(event['windows']))) + '\n'
                                       for event in self.events)
        old_summary = self.scan(self.write_commit(old), 1)
        self.assertIn('Hex High Entropy String', old_summary['finding_types'])
        new = policy.render('# quarantine\n', self.events, PROJECT, NOW)
        new_summary = self.scan(self.write_commit(new), 0)
        self.assertEqual(new_summary['findings'], 0)
        self.proof(old=old_summary, new=new_summary, source_families=8)

    def test_ac2_evidence_compatibility(self):
        legacy_sha = hashlib.sha1(b'unmanaged historical evidence').hexdigest()
        unmanaged = 'tests/test_manual.py::test_manual\toperator\t2026-11-14\told evidence=' + legacy_sha
        expired = 'tests/test_expired.py::test_expired\toperator\t2026-11-06\told evidence=ruling:old'
        text = '# existing comment\n\n' + unmanaged + '\n' + expired + '\n'
        # A managed row is reconciled while historical unmanaged evidence stays exact.
        text += self.events[0]['node'] + '\told-owner\t2026-11-14\told evidence=drop-old\n'
        snapshot = copy.deepcopy(self.events)
        rendered = policy.render(text, self.events, PROJECT, NOW)
        self.assertEqual(self.events, snapshot, 'ledger source identities changed')
        self.assertEqual(rendered, policy.render(text, self.events, PROJECT, NOW))
        self.assertEqual(rendered, policy.render(rendered, self.events, PROJECT, NOW))
        self.assertTrue(rendered.startswith('# existing comment\n\n' + unmanaged + '\n'))
        self.assertNotIn(expired, rendered)
        self.assertNotIn('old-owner', rendered)
        path = self.repo / policy.PATH
        path.write_text(rendered)
        rows = self.reader.quarantine_rows(str(path))
        self.assertEqual(self.reader.load_quarantine(str(path), today=NOW.date()),
                         {e['node'] for e in self.events} | {'tests/test_manual.py::test_manual'})
        by_node = {row['node']: row for row in rows}
        for event in self.events:
            row = by_node[event['node']]
            self.assertEqual(row['owner'], event['owner'])
            self.assertEqual(row['expiry'], '2026-11-14')
            expected = [re.sub(r'(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])',
                               lambda match: match[0][:7], source) for source in event['windows']]
            self.assertEqual(row['reason'], event['rule'] + ' evidence=' + ','.join(expected))
        # The same window retains two separately identifiable, valid JSON rehearsals.
        reason = by_node[self.events[3]['node']]['reason']
        refs = reason.split(' evidence=')[1].split(',tree:')
        first, second = json.loads(refs[0][5:]), json.loads(refs[1])
        self.assertEqual(first[0], second[0])
        self.assertNotEqual(first[1:], second[1:])
        self.assertEqual(first[1], json.loads(self.events[3]['windows'][0][5:])[1][:7])
        # Legacy long-SHA and already short/ruling rows remain reader-compatible.
        self.assertIn(legacy_sha, by_node['tests/test_manual.py::test_manual']['reason'])
        self.assertIn('ruling:L-operator-0001.jsonl:42', rendered)
        self.assertIn(self.events[7]['windows'][0], rendered)
        for field in ('owner', 'rule', 'windows'):
            for control in ('\t', '\r', '\n', '\x00', '\x7f', '\x80', '\u2028', '\u2029'):
                bad = copy.deepcopy(self.events[:1])
                bad[0][field] = ['bad' + control + 'row'] if field == 'windows' else 'bad' + control + 'row'
                with self.assertRaises(ValueError, msg=field + ' control metadata accepted'):
                    policy.render('', bad, PROJECT, NOW)
        self.proof(rows=len(rows), source_references_distinct=True, legacy_readable=True,
                   ledger_unchanged=True, deterministic=True, control_metadata_rejected=True)

    def test_ac3_committed_policy(self):
        original_status = self.git('status', '--porcelain')
        original_policy = (self.repo / policy.PATH).read_bytes()
        commit = policy.candidate(self.repo, self.base, self.events, PROJECT, now=NOW)
        self.assertNotEqual(commit, self.base)
        self.assertEqual(self.git('rev-parse', commit + '^'), self.base)
        # git show is a separate process reading the writer's returned commit.
        readback = subprocess.run(['git', '-C', str(self.repo), 'show', commit + ':' + policy.PATH],
                                  capture_output=True, timeout=60)
        self.assertEqual(readback.returncode, 0)
        expected = policy.render(original_policy.decode(), self.events, PROJECT, NOW).encode()
        self.assertEqual(readback.stdout, expected)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.git('status', '--porcelain'), original_status)
        self.assertEqual((self.repo / policy.PATH).read_bytes(), original_policy)
        self.assertEqual(policy.candidate(self.repo, commit, self.events, PROJECT, now=NOW), commit)
        self.assertEqual(policy.candidate(self.repo, self.base, self.events, 'other', now=NOW), self.base)
        # Read committed rows through the real reader in another interpreter.
        checkout = Path(self.scratch.name) / 'readback'
        self.git('worktree', 'add', '--detach', str(checkout), commit)
        self.assertEqual((checkout / policy.PATH).read_bytes(), readback.stdout)
        reader = subprocess.run([sys.executable, '-c',
            'import sys,json; from datetime import date; '
            'sys.path.insert(0,sys.argv[1]); import pytest_baseline_gate as g; '
            'print(json.dumps([g.quarantine_rows(sys.argv[2]), '
            'sorted(g.load_quarantine(sys.argv[2], date(2026,11,7)))]))',
            str(checkout / 'scripts/ci'), str(checkout / policy.PATH)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(reader.returncode, 0)
        rows, nodes = json.loads(reader.stdout)
        self.assertEqual(set(nodes), {event['node'] for event in self.events})
        self.assertEqual(len(rows), len(self.events))
        # Scan the returned commit's own checkout, leaving the original untouched.
        original_repo = self.repo
        try:
            self.repo = checkout
            summary = self.scan(commit, 0)
        finally:
            self.repo = original_repo
        self.assertEqual(self.git('status', '--porcelain'), original_status)
        self.assertEqual((self.repo / policy.PATH).read_bytes(), original_policy)
        self.assertEqual(summary['findings'], 0)
        self.proof(committed_policy_readback=True, checkout_untouched=True, idempotent=True,
                   separate_process_rows=len(rows), scanner=summary)


if __name__ == '__main__':
    unittest.main()
