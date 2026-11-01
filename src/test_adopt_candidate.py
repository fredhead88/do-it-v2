"""Adoption uses real disposable repos, ledger appends and grader packets."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import unittest

import harness


DOIT = Path(__file__).resolve().parents[1] / 'doit'
SPEC = 'L-spec-1945'


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.root = harness.test_root('adopt-candidate')
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith('GIT_') and k not in ('DOIT_LEDGER_FILE', 'DOIT_PROJECT')}
        self.env.update(HOME=str(self.root / 'home'), DOIT_ROOT=str(self.root),
                        XDG_CONFIG_HOME=str(self.root / 'xdg'),
                        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.test',
                        GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.test')
        self.repo = self.root / 'repos' / 'do-it-v2'
        self.wt = self.root / 'worktrees' / 'do-it-v2' / SPEC.lower()
        self.repo.mkdir(parents=True)
        self.git(self.repo, 'init', '-q', '--initial-branch=main')
        (self.repo / 'file').write_text('base\n')
        self.git(self.repo, 'add', 'file')
        self.git(self.repo, 'commit', '-qm', 'base')
        self.base = self.git(self.repo, 'rev-parse', 'HEAD')
        self.wt.parent.mkdir(parents=True)
        self.git(self.repo, 'worktree', 'add', '-qb', SPEC.lower(), str(self.wt))
        self.ready = self.commit(self.wt, 'candidate\n')
        content = self.root / 'content'
        content.mkdir()
        self.spec = content / (SPEC + '.md')
        self.spec.write_text('''# Adoption fixture
## Goal
Preserve the candidate behavior.
## Interfaces
Writes: file
## Verification
```bash
/usr/bin/python3 -c 'print("verified")' && echo "$BASE"
```
## Acceptance criteria
AC1 [backend]: the adopted candidate preserves the required behavior.
  review_path: read the fixture output; pass when the behavior is preserved.
''')
        self.card = content / ('L-card-1945.md')
        self.card.write_text('''# Build card
AC1 [backend] met · fixture evidence
verify exit 0
''')
        self.events = self.root / 'events'
        self.events.mkdir()
        self.seed('spec-writer', 'spec-written', path=str(self.spec), footprint=['file'])
        self.seed('builder', 'build-done', branch=SPEC.lower(), base_sha=self.base,
                  ready_sha=self.base, card=str(self.card), status='BLOCKED', verify_exit=4)

    def tearDown(self):
        harness.cleanup(self.root)

    def git(self, repo, *args):
        return subprocess.check_output(['git', '-C', str(repo), *args],
                                       env=self.env, text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self, repo, text):
        (repo / 'file').write_text(text)
        self.git(repo, 'add', 'file')
        self.git(repo, 'commit', '-qm', text.strip())
        return self.git(repo, 'rev-parse', 'HEAD')

    def seed(self, actor, kind, **fields):
        event = dict(v=1, ts='2026-01-01T00:00:00+00:00', type=kind,
                     subject=SPEC, project='do-it-v2', **fields)
        with (self.events / f'L-{actor}-fixture.jsonl').open('a') as f:
            f.write(json.dumps(event) + '\n')

    def run_adopt(self, sha=None, env=None):
        return subprocess.run([str(DOIT), 'adopt-candidate', SPEC, sha or self.ready,
                               '--by', 'thinker', '--reason', 'hand-resolved conflicts'],
                              env=env or self.env, capture_output=True, text=True, timeout=30)

    def snapshot(self):
        return (self.git(self.repo, 'rev-parse', 'HEAD'),
                self.git(self.wt, 'status', '--porcelain'),
                (self.wt / 'file').read_text(),
                {p.name: p.read_bytes() for p in self.events.glob('*.jsonl')})

    def assert_refused(self, expected, sha=None):
        before = self.snapshot()
        result = self.run_adopt(sha)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(expected, result.stderr)
        self.assertEqual(self.snapshot(), before)

    def adopted(self):
        return [json.loads(line) for p in self.events.glob('*.jsonl')
                for line in p.read_text().splitlines()
                if json.loads(line).get('adopted_by')]

    def packet(self):
        return subprocess.run([str(DOIT), 'packet', 'grader', SPEC, '--project', 'do-it-v2',
                               '--worktree', str(self.wt), '--repo', str(self.repo)],
                              env=self.env, capture_output=True, text=True, timeout=30)

    def test_adopt_ref_move_reconciles_only_owned_worktree_and_emits_one_build(self):
        # The branch was moved by hand while its index still describes the old base.
        self.git(self.wt, 'reset', '--hard', self.base)
        self.git(self.repo, 'update-ref', 'refs/heads/' + SPEC.lower(), self.ready)
        other = self.root / 'other'
        self.git(self.repo, 'worktree', 'add', '-qb', 'other', str(other), self.base)
        (other / 'file').write_text('other uncommitted work\n')
        integrator = (self.git(self.repo, 'symbolic-ref', 'HEAD'),
                      self.git(self.repo, 'status', '--porcelain'), (self.repo / 'file').read_bytes())
        other_before = self.git(other, 'status', '--porcelain'), (other / 'file').read_bytes()
        # Caller-selected ledger destinations must not own this tool's event.
        result = self.run_adopt(self.ready[:7], {**self.env, 'DOIT_LEDGER_FILE': 'L-grader-wrong.jsonl'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.wt / 'file').read_text(), 'candidate\n')
        self.assertEqual(self.git(self.wt, 'status', '--porcelain'), '')
        self.assertEqual((self.git(self.repo, 'symbolic-ref', 'HEAD'),
                          self.git(self.repo, 'status', '--porcelain'),
                          (self.repo / 'file').read_bytes()), integrator)
        self.assertEqual((self.git(other, 'status', '--porcelain'),
                          (other / 'file').read_bytes()), other_before)
        events = self.adopted()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual((e['type'], e['status'], e['ready_sha'], e['base_sha']),
                         ('build-done', 'DONE', self.ready, self.base))
        self.assertIsNone(e['verify_exit'])
        self.assertTrue(e['base_sha_explicit'])
        self.assertEqual(e['adopted_by'], 'thinker')
        self.assertEqual(e['reason'], 'hand-resolved conflicts')
        self.assertEqual(e['card'], str(self.card))
        self.assertTrue((self.events / 'L-adopt-candidate-local.jsonl').exists())
        self.assertFalse((self.events / 'L-grader-wrong.jsonl').exists())

    def test_refuse_non_descendant(self):
        self.commit(self.repo, 'master advanced\n')
        self.assert_refused('not a descendant')

    def test_refuse_multiple_commits(self):
        self.ready = self.commit(self.wt, 'second change\n')
        self.assert_refused('exactly one commit')

    def test_refuse_zero_commits(self):
        self.git(self.wt, 'reset', '--hard', self.base)
        self.assert_refused('exactly one commit', self.base)

    def test_refuse_sha_that_is_not_branch_tip(self):
        self.assert_refused('branch tip', self.base)

    def test_existing_window_base_is_supported(self):
        self.git(self.repo, 'branch', 'window-fixture', self.base)
        self.commit(self.repo, 'new master\n')
        self.spec.write_text('Base: window-fixture\n' + self.spec.read_text())
        result = self.run_adopt()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.adopted()[0]['base_sha'], self.base)

    def test_refuse_worktree_redirected_to_integrator(self):
        self.git(self.repo, 'worktree', 'remove', str(self.wt))
        self.wt.symlink_to(self.repo, target_is_directory=True)
        before = self.git(self.repo, 'status', '--porcelain')
        result = self.run_adopt()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('worktree', result.stderr)
        self.assertEqual(self.git(self.repo, 'status', '--porcelain'), before)
        self.assertEqual(self.adopted(), [])

    def test_refuse_wrong_worktree_branch(self):
        self.git(self.wt, 'switch', '-qc', 'unrelated')
        self.assert_refused('expected branch')

    def test_refuse_in_progress_operation(self):
        merge_head = self.git(self.wt, 'rev-parse', '--git-path', 'MERGE_HEAD')
        Path(merge_head).write_text(self.base + '\n')
        self.assert_refused('git operation')

    def test_refuse_concurrent_dispatch(self):
        state = self.root / 'state'
        state.mkdir()
        with (state / f'dispatch-{SPEC}.lock').open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_refused('subject lock')

    def test_inherited_git_routing_cannot_redirect_adoption(self):
        env = {**self.env, 'GIT_DIR': str(self.repo / '.git'),
               'GIT_WORK_TREE': str(self.repo)}
        result = self.run_adopt(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.adopted()[0]['ready_sha'], self.ready)

    def test_adoption_removes_stale_routes_and_packet_uses_new_base(self):
        self.seed('grader', 'criterion-routed', criterion='AC1', capability='browser', to='reviewer')
        before = self.packet()
        self.assertNotEqual(before.returncode, 0)
        self.assertIn('empty grader criteria', before.stderr)
        result = self.run_adopt()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.packet()
        self.assertEqual(after.returncode, 0, after.stderr)
        text = Path(after.stdout.strip()).read_text()
        self.assertIn('AC1 [backend]: the adopted candidate', text)
        verify = self.root / 'content' / f'verify-{SPEC}-grader.sh'
        self.assertIn(self.base, verify.read_text())


if __name__ == '__main__':
    unittest.main()
