#!/usr/bin/env python3
"""Hermetic how-to regressions; also run by doit test."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import howto

REPO = Path(__file__).resolve().parent.parent
LITERALS = {'spawn-stale', 'spawn-failed', 'charter-proving', 'charter-reopened', 'charter-closed',
            'worktree-reaped', 'tick', 'cron-missing', 'look-stale',
            'proving-check-failed', 'proving-overdue', 'autodispatch-failed',
            'autodispatch-skipped', 'autodispatched'}


class HowtoTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name)
        for folder in ('agents', 'src', 'docs'):
            (self.repo / folder).mkdir()
        self.write('agents/worker.md', 'worker')
        self.write('agents/rowless.md', 'rowless')
        self.write('models.example.toml', '[defaults]\nbackend="seat"\n'
                   '[contracts.worker]\nbackend="codex"\nmodel="fixture"\n')
        self.write('lane_rules.toml', '[[rules]]\nname="build"\n'
                   'action="dispatch-builder"\nactor="tick"\nlimit_min=30\n')
        self.write('crons.toml', '[[row]]\nname="tick"\nschedule="*/{tick_min} * * * *"\n'
                   'where="user"\ncommand="{doit} tick >> {root}/tick.log"\npath="{repo}/doit"\n')
        self.write('look.toml', '[merge_queue]\nmember_verify_parallel = 4\nverdict_fresh_hours = 24\n')
        self.write('src/tick.py', 'dispatch.emit(path(), {}, "spawn-stale")\n'
                   'look.emit_once("cron-missing", "key", "actor", {}, [])\n'
                   'other.emit("not-an-event")\n')

    def write(self, name, body):
        (self.repo / name).write_text(body)

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = howto.main(list(args), repo=self.repo)
        return code, output.getvalue()

    def test_ac1_sources_defaults_and_environment(self):
        page = howto.render(str(self.repo))
        for fact in ('worker: codex', 'rowless: seat (default)', 'dispatch-builder',
                     'spawn-stale', 'cron-missing', '*/{tick_min} * * * *',
                     '{doit} tick >> {root}/tick.log', '{repo}/doit'):
            self.assertIn(fact, page)
        self.assertNotIn('not-an-event', page)
        self.assertNotIn('unrouted', page)
        with patch.dict(os.environ, DOIT_TICK_MIN='2', DOIT_ROOT='/fixture/first'):
            first = howto.render(str(REPO))
        with patch.dict(os.environ, DOIT_TICK_MIN='19', DOIT_ROOT='/fixture/second'):
            self.assertEqual(first, howto.render(str(REPO)))
        self.assertEqual(first, howto.render(str(REPO)))
        source = REPO / 'machine/models.toml'
        if not source.exists():
            source = REPO / 'models.example.toml'
        models = tomllib.loads(source.read_text())
        for role in (REPO / 'agents').glob('*.md'):
            contract = models.get('contracts', {}).get(role.stem, {})
            backend = contract.get('backend', models['defaults']['backend'])
            suffix = '' if 'backend' in contract else ' (default)'
            self.assertIn(f'- {role.stem}: {backend}{suffix}', first)
        rules = tomllib.loads((REPO / 'lane_rules.toml').read_text())['rules']
        self.assertEqual(howto.event_types(REPO / 'src/tick.py'), LITERALS | {'next-step-held', 'escalation-blocking', 'problem-occurred'})
        normal = ' '.join(first.split())
        for row in rules:
            self.assertIn(' | '.join(str(row[k]) for k in
                                    ('name', 'action', 'actor', 'limit_min')), normal)
        for row in tomllib.loads((REPO / 'crons.toml').read_text())['row']:
            self.assertIn(' | '.join(row[k] for k in ('name', 'schedule', 'where', 'path')), normal)
            self.assertIn(row['command'], normal)
        if source.name == 'models.example.toml':
            self.assertIn('relay: seat (default)', first)
            self.assertIn('owed-verifier: seat (default)', first)
        (self.repo / 'machine').mkdir()
        self.write('machine/models.toml', '[defaults]\nbackend="codex"\n')
        self.assertIn('rowless: codex (default)', howto.render(str(self.repo)))
        print('1500-AC1 fixture, real repo, both emit forms, environment ... ok')

    def test_member_verdict_section_follows_look_toml(self):
        page = howto.render(str(self.repo))
        self.assertIn('## Member verdicts', page)
        self.assertIn('`src/window_dispatch.py`: predeploy tree, window, suite, quick ids', ' '.join(page.split()))
        self.assertIn('`src/window_known_red.py`: `window-dispatch-verdict` completion receipts', ' '.join(page.split()))
        self.assertIn('at most 4 CI runs at once', page)
        self.assertIn('reused for 24 hours', ' '.join(page.split()))
        self.write('look.toml', '[merge_queue]\nmember_verify_parallel = 2\nverdict_fresh_hours = 6\n')
        again = ' '.join(howto.render(str(self.repo)).split())
        self.assertIn('at most 2 CI runs at once', again)
        self.assertIn('reused for 6 hours', again)
        self.assertIn('`src/prequeue.py` (`doit prequeue`)', again)

    def test_token_health_text(self):
        page = howto.render(str(REPO))
        for marker in ('tokens = {vendor, input, cached_input, output, reasoning}',
                       'doit health', 'unmeasured_specs', 'unknown', 'claude-sonnet-5-5',
                       'claude-opus-5-5', '14 days', 'axis=K'):
            self.assertIn(marker, page)

    def test_replan_queue_entry(self):
        page = howto.render(str(REPO))
        for marker in ('oldest-verdict replans, two active slots', 'holds retain queue entries',
                       '`charter-replan`: tick-only', 'planner/thinker/operator with nonblank why',
                       'strict later release', 'hold/capacity waits preserve existing intake gates'):
            self.assertIn(marker, page)

    def test_base_failure_handoff_entry(self):
        page = howto.render(str(REPO))
        for marker in ('DONE+notes', 'BLOCKED+base_only/cited',
                       'current proof with `n_new=0`', 'existing gates apply'):
            self.assertIn(marker, page)

    def test_direct_merge_entry(self):
        page = ' '.join(howto.render(str(REPO)).split())
        for marker in ('src/direct_merge.py', 'merge_one', 'one do-it-v2 queue row',
                       'dirty paths', 'unchanged main/branch shas', 'merge --no-ff',
                       'emit shipped', 'failed merge is aborted', 'cli_gate'):
            self.assertIn(marker, page)

    def test_ac2_mutation_and_deletion(self):
        before = howto.render(str(self.repo))
        sources = {name: (self.repo / name).read_text()
                   for name in ('lane_rules.toml', 'crons.toml', 'src/tick.py')}
        with (self.repo / 'lane_rules.toml').open('a') as stream:
            stream.write('[[rules]]\nname="extra"\naction="dispatch-builder"\n'
                         'actor="operator"\nlimit_min=9\n')
        with (self.repo / 'crons.toml').open('a') as stream:
            stream.write('[[row]]\nname="extra-cron"\nschedule="0 1 * * *"\n'
                         'where="cron.d"\ncommand="extra-command"\npath="extra-path"\n')
        self.write('agents/extra-role.md', 'extra')
        with (self.repo / 'src/tick.py').open('a') as stream:
            stream.write('look.emit_once("extra-event", "key", "actor", {}, [])\n')
        after = howto.render(str(self.repo))
        added = ['extra | dispatch-builder | operator | 9',
                 'extra-cron | 0 1 * * * | cron.d | extra-path',
                 '  extra-command', 'extra-role: seat (default)', 'extra-event']
        # Remove exactly the new source facts, ignoring wrapping and bullet separators.
        def words(text):
            return text.replace(';', '').replace('- ', '').split()
        remainder = after
        for fact in added:
            self.assertIn(fact, remainder)
            remainder = remainder.replace(fact, '')
        self.assertEqual(words(before), words(remainder))
        (self.repo / 'agents/extra-role.md').unlink()
        self.assertNotIn('extra-role', howto.render(str(self.repo)))
        for name, body in sources.items():
            self.write(name, body)
        self.assertEqual(before, howto.render(str(self.repo)))
        print('1500-AC2 four source additions and agent deletion ... ok')

    def test_migration_cut_entry(self):
        page = howto.render(str(REPO))
        self.assertIn('migration-not-cut', page)
        self.assertIn('migration-only predecessor first', page)

    def test_shared_readiness_disk_entry(self):
        page = howto.render(str(REPO))
        for fact in ('begin_pass: one scratch disk sample', 'before optional load admission',
                     'disk holds survive load off/errors', '5/2 GiB',
                     'admission.json holding', 'dry runs never save',
                     'Receiver alone reclaims and rechecks'):
            self.assertIn(fact, page)

    def test_ac3_page_budget(self):
        for repo in (self.repo, REPO):
            page = howto.render(str(repo))
            self.assertLessEqual(len(page.splitlines()), 150)
            self.assertTrue(all(len(line) <= 100 for line in page.splitlines()))
            self.assertIn('doit howto', page.split('\n\n')[1])
            self.assertEqual(page.count('\n## '), 6)
            self.assertIn('## Branch refresh', page)
        print('1500-AC3 header, six sections, 136 lines, 100 columns ... ok')

    def test_ac4_command_states_and_no_partial_write(self):
        self.assertEqual(self.call()[0], 0)
        target = self.repo / 'docs/HOW-IT-WORKS.md'
        content = target.read_bytes()
        stamp = target.stat().st_mtime_ns
        self.assertEqual(self.call('--check')[0], 0)
        self.assertEqual(target.stat().st_mtime_ns, stamp)
        target.write_bytes(content + b'drift\n')
        drift_stamp = target.stat().st_mtime_ns
        code, output = self.call('--check')
        self.assertEqual(code, 1)
        self.assertIn('--- ', output)
        self.assertIn('+++ ', output)
        self.assertEqual(target.stat().st_mtime_ns, drift_stamp)
        for source in ('lane_rules.toml', 'models.example.toml', 'crons.toml', 'src/tick.py'):
            path = self.repo / source
            original = path.read_text()
            path.unlink()
            self.assertEqual(self.call('--check')[0], 2, source)
            self.assertEqual(self.call()[0], 2, source)
            path.write_text('[[[')
            self.assertEqual(self.call('--check')[0], 2, source)
            self.assertEqual(self.call()[0], 2, source)
            self.assertEqual(target.read_bytes(), content + b'drift\n')
            path.write_text(original)
        for malformed in ('', '[[rules]]\nname="incomplete"\n'):
            self.write('lane_rules.toml', malformed)
            self.assertEqual(self.call('--check')[0], 2)
        target.unlink()
        self.assertEqual(self.call('--check')[0], 2)
        print('1500-AC4 equal, drift, missing, malformed; check writes nothing ... ok')

    def test_paused_cron_is_named_in_generated_document(self):
        source = self.repo / 'crons.toml'
        source.write_text(source.read_text() + 'paused=true\n')
        self.assertIn(' (paused)', howto.render(str(self.repo)))

    def test_ac5_committed_page_and_shell_entrypoint(self):
        self.assertIn('tombstones veto verdicts', howto.render(str(REPO)))
        self.assertEqual((REPO / 'docs/HOW-IT-WORKS.md').read_text(), howto.render(str(REPO)))
        result = subprocess.run([str(REPO / 'doit'), 'howto', '--check'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        print('1500-AC5 committed page and doit howto --check ... ok')


if __name__ == '__main__':
    unittest.main()
