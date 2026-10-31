"""Corrective selector proofs using disposable ledgers and stubbed gate spending."""
import atexit
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_boot = tempfile.TemporaryDirectory(prefix='fill-import-')
atexit.register(_boot.cleanup)
os.environ['DOIT_ROOT'] = _boot.name
os.environ['DOIT_NO_POKE'] = '1'
import dispatch
import fold
import merge_queue as mq
import precheck

SRC = Path(__file__).resolve().parent


class WindowFillCorrectiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='window-fill-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'events').mkdir()
        self.config = self.root / 'look.toml'
        self.config.write_text('[merge_queue]\nbatch_cap=15\n')
        self.now = datetime.now(timezone.utc)
        self.stamp = self.now - timedelta(seconds=10)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module, name, value in ((fold, 'ROOT', self.root), (fold, 'EVENTS', self.root / 'events'),
                                    (mq, '_look_toml', lambda: self.config),
                                    (dispatch, 'now', lambda: self.stamp.isoformat())):
            self.stack.enter_context(patch.object(module, name, value))
        self.stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1',
                                           DOIT_LEDGER_FILE='L-executor-0001.jsonl', DOIT_WINDOW_MAX_MERGES='15'))
        self.reports = []

    def emit(self, kind, sid, **fields):
        if kind == 'precheck-red':
            fields = dict(fields, tests=fields.pop('nodes', ['guard']), master='fixture', branch_sha='fixture')
        dst = self.root / 'events/L-executor-0001.jsonl'
        self.assertIsNone(dispatch.emit(dst, {'subject': sid}, kind, **fields))

    def seed(self, count, cap=15):
        for path in (self.root / 'events').glob('*.jsonl'):
            path.unlink()
        for path in (self.root / 'state').glob('*'):
            path.unlink()
        self.config.write_text('[merge_queue]\nbatch_cap=' + str(cap) + '\n')
        self.stamp = self.now - timedelta(seconds=10)
        ids = [f'S{i:02}' for i in range(count)]
        for sid in ids:
            self.emit('queue-add', sid, project='proj', branch=sid, rule='operator')
        self.stamp = self.now
        return ids

    def fresh(self):
        code = 'import fold,json; print(json.dumps(fold.read_events(use_cache=False)))'
        env = dict(os.environ, PYTHONPATH=str(SRC), DOIT_ROOT=str(self.root))
        result = subprocess.run(['/usr/bin/python3', '-B', '-c', code], env=env,
                                text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def run_case(self, name, *, paths=None, checked=None, detached=False, real=False,
                 before=None, code=0, repository=True, synchronous=False):
        calls = []
        before_rows = self.fresh()
        def gate(argv, **kwargs):
            landed = self.fresh()  # Independent committed read BEFORE spending.
            batch = [e for e in landed if e['type'] == 'queue-batch'][-1]
            self.assertEqual(argv[-1], batch['specs'])
            self.assertEqual(batch['specs'], ','.join(batch['taken']))
            calls.append(argv)
            if before:
                before(argv)
            return type('Child', (), {'pid': os.getpid()})() if detached else {'code': code}
        out, err = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            if not real:
                stack.enter_context(patch.object(mq, '_repository', **(
                    {'return_value': (self.root, 'master')} if repository else
                    {'side_effect': ValueError('missing repository')})))
                stack.enter_context(patch.object(mq, 'spec_paths', side_effect=lambda r, m, b:
                    [('M', b)] if paths is None else paths[b]))
            if synchronous:
                stack.enter_context(patch.object(precheck, 'master_sha', return_value='fixture'))
            else:
                stack.enter_context(patch.object(precheck, 'selection', side_effect=checked if callable(checked)
                                                 else None, return_value=checked if not callable(checked) else None))
            with redirect_stdout(out), redirect_stderr(err):
                rc = mq.window_run('proj', now=self.now, precheck_runner=checked if synchronous else None, **(
                    {'detach': True, 'launcher': gate} if detached else {'runner': gate}))
        rows = self.fresh()
        batches = [e for e in rows if e['type'] == 'queue-batch']
        batch = batches[-1] if batches else None
        report = dict(case=name, input=[r['spec'] for r in mq.queue(before_rows, 'proj')],
                      code=rc, taken=batch['taken'] if batch else [],
                      skipped=batch['skipped'] if batch else [], stderr=err.getvalue())
        self.reports.append(report)
        return rc, batch, rows, calls, out.getvalue(), err.getvalue()

    def report(self, ac):
        body = json.dumps(self.reports, sort_keys=True)
        print(json.dumps(dict(url='scratch://window-fill/' + ac, status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def partition(self, batch, rows):
        queued = [r['spec'] for r in mq.queue(rows, 'proj')]
        explained = batch['taken'] + [s['spec'] for s in batch['skipped']]
        self.assertEqual(batch['queue_len'], len(queued))
        self.assertEqual(len(explained), len(set(explained)))
        self.assertCountEqual(explained, queued)

    def test_ac1_capacity_order(self):
        import tomllib
        installed = tomllib.loads((SRC.parent / 'look.toml').read_text())
        self.assertEqual(installed['merge_queue']['batch_cap'], 15)
        self.assertEqual(installed['window_gate']['max_merges'], 15)
        for detached in (False, True):
            for count, cap in [(n, 15) for n in (0, 1, 2, 14, 15, 16, 40)] + [(40, n) for n in (1, 6, 15)]:
                ids = self.seed(count, cap)
                rc, batch, rows, calls, *_ = self.run_case(f'capacity-{count}-{cap}-{detached}', detached=detached)
                self.assertEqual(rc, 0 if count else 3)
                self.assertEqual(batch['taken'] if batch else [], ids[:cap])
                if batch:
                    self.partition(batch, rows)
        ids = self.seed(40)
        # Equal timestamps preserve ledger ties; differing ages stay FIFO in each tier.
        for i, sid in enumerate(ids):
            dst = self.root / 'events/L-spec-writer-local.jsonl'
            self.assertIsNone(dispatch.emit(dst, {'subject': sid}, 'spec-written',
                charter=['L-charter-other', 'L-charter-0074', 'L-charter-0072'][i % 3]))
        self.stamp += timedelta(seconds=1)
        self.emit('queue-add', ids[2], project='proj', branch=ids[2], rule='operator')
        expected = ids[5::3] + [ids[2]] + ids[1::3] + ids[0::3]
        paths = {sid: [('M', sid)] for sid in ids}
        paths[ids[5]] = [('M', 'deploy.sh')]
        paths[ids[20]] = [('A', 'alembic/versions/new.py')]
        checked = dict(ts=self.now.isoformat(), clean=ids[-2:][::-1], conflict_set={ids[5]: 'diagnostic-only'})
        rc, batch, *_ = self.run_case('tier-age-ties-risk-clean-subset', paths=paths, checked=checked)
        self.assertEqual(batch['taken'], expected[:15])
        for generated in (False, True):
            ids = self.seed(40)
            def red(*args):
                self.emit('precheck-red', ids[0], project='proj', nodes=['guard'])
                return dict(master='fixture', ts=self.stamp.isoformat(), clean=ids[1:], guard_red={ids[0]: ['guard']})
            if not generated:
                red()
            rc, batch, *_ = self.run_case('guard-red-generated-' + str(generated), checked=red if generated else None, synchronous=generated)
            self.assertEqual(batch['taken'], ids[1:16])
            self.assertEqual(batch['skipped'][0]['reason'], 'precheck-red')
        ids = self.seed(40)
        checked = dict(ts=self.now.isoformat(), clean=ids[-2:], guard_red={s: ['guard'] for s in ids[:-2]})
        _, batch, *_ = self.run_case('38-attributed-guard-red', checked=checked)
        self.assertEqual(batch['taken'], ids[-2:])
        ids = self.seed(40)
        def terminal(*args):
            self.emit('merge-conflict', ids[0], project='proj', reason='master')
            return dict(ts=self.now.isoformat(), clean=ids[1:])
        _, batch, rows, *_ = self.run_case('terminal-refresh', checked=terminal)
        self.assertEqual(batch['taken'], ids[1:16])
        self.assertEqual(batch['queue_len'], 39)
        self.assertTrue(any(e['type'] == 'queue-remove' and e['reason'] == 'merge-conflict' for e in rows))
        ids = self.seed(40)
        _, batch, *_ = self.run_case('returned-master-conflict', checked=dict(ts=self.now.isoformat(),
                         conflict_master={ids[0]: 'shared'}, clean=[]))
        self.assertEqual(batch['taken'], ids[1:16])
        self.assertEqual(batch['skipped'][0]['reason'], 'conflict-master')
        self.report('AC1')

    def test_ac2_conflict_backfill(self):
        ids = self.seed(18)
        paths = {s: [('M', s)] for s in ids}
        paths[ids[1]] = paths[ids[0]]
        _, batch, rows, *_ = self.run_case('position-two-backfill', paths=paths)
        self.assertEqual(batch['taken'], [ids[0]] + ids[2:16])
        self.assertEqual(batch['skipped'][0], dict(spec=ids[1], reason='footprint-conflict', conflicts_with=[ids[0]], paths=[ids[0]]))
        self.assertEqual(len(mq.queue(rows)), 18)
        ids = self.seed(6)
        paths = dict(zip(ids, [[('D', 'a')], [('A', 'a'), ('M', 'b')], [('M', 'b')], [],
                               [('M', 'a'), ('M', 'b')], [('M', 'z')]]))
        _, batch, *_ = self.run_case('chain-empty-delete-add-multiple-blockers-exhausted', paths=paths)
        self.assertEqual(batch['taken'], [ids[i] for i in (0, 2, 3, 5)])
        self.assertEqual(batch['skipped'][1]['conflicts_with'], [ids[0], ids[2]])
        self.assertEqual(batch['skipped'][1]['paths'], ['a', 'b'])
        ids = self.seed(3)
        paths = {ids[0]: [], ids[1]: None, ids[2]: []}
        rc, batch, rows, calls, *_ = self.run_case('unknown-footprint', paths=paths)
        self.assertEqual((rc, batch, calls), (3, None, []))
        refusal = [e for e in rows if e['type'] == 'window-refused'][-1]
        self.assertEqual((refusal['reason'], refusal['specs']), ('footprint-undetermined', [ids[1]]))
        self.emit('precheck-red', ids[1], project='proj', nodes=['guard'])
        _, batch, *_ = self.run_case('red-needs-no-footprint', paths=paths)
        self.assertEqual(batch['taken'], [ids[0], ids[2]])
        ids = self.seed(3)
        repo = self.make_repo(ids, shared_newline=True)
        self.assertIn(('A', 'line\nbreak'), mq.spec_paths(repo, 'master', ids[0]))
        _, batch, *_ = self.run_case('real-git-newline-conflict', real=True)
        self.assertEqual(batch['taken'], [ids[0], ids[2]])
        self.assertEqual(batch['skipped'][0]['paths'], ['line\nbreak'])
        self.report('AC2')

    def test_ac3_ledger_roundtrip(self):
        for count, shared, red in ((18, False, False), (5, True, False), (1, False, False), (20, False, True)):
            ids = self.seed(count)
            paths = {s: [('M', 'shared' if shared else s)] for s in ids}
            if red:
                self.emit('precheck-red', ids[-1], project='proj', nodes=['guard'])
            checked = dict(ts=self.now.isoformat(), clean=[], conflict_master={ids[-2]: 'master'}) if red else None
            _, batch, rows, calls, *_ = self.run_case(f'roundtrip-{count}-{shared}-{red}', paths=paths, checked=checked)
            self.partition(batch, rows)
            self.assertEqual(batch['cap'], 15)
            self.assertEqual(calls[0][-1], ','.join(batch['taken']))
            self.assertTrue(all(r['conflicts_with'] == [] and r['paths'] == []
                                for r in batch['skipped'] if r['reason'] != 'footprint-conflict'))
            if red:
                reasons = {r['spec']: r['reason'] for r in batch['skipped']}
                self.assertEqual(reasons[ids[-1]], 'precheck-red')
                self.assertEqual(reasons[ids[-2]], 'conflict-master')
                self.assertEqual(reasons[ids[15]], 'cap-reached')
            self.assertEqual(len(mq.queue(rows)), count)  # additive fields accepted by legacy reader
            self.assertIsInstance(mq._launching(rows, 'proj', []), list)
        ids = self.seed(3)
        def terminal(*args):
            self.emit('merge-conflict', ids[0], project='proj', reason='master')
        _, batch, rows, *_ = self.run_case('roundtrip-terminal-removal', checked=terminal)
        self.partition(batch, rows)
        self.assertEqual(batch['queue_len'], 2)
        self.assertEqual([e['subject'] for e in rows if e['type'] == 'queue-remove'], [ids[0]])
        self.report('AC3')

    def git(self, repo, *args):
        return subprocess.run(['git', *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()

    def make_repo(self, ids, shared_newline=False):
        repo = self.root / 'repos/proj'
        repo.mkdir(parents=True)
        self.git(repo, 'init', '-q', '-b', 'master')
        self.git(repo, 'config', 'user.name', 'fixture')
        self.git(repo, 'config', 'user.email', 'fixture@localhost')
        (repo / 'base').write_text('base')
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-qm', 'base')
        for i, sid in enumerate(ids):
            self.git(repo, 'checkout', '-qb', sid, 'master')
            (repo / ('line\nbreak' if shared_newline and i < 2 else sid)).write_text(sid)
            self.git(repo, 'add', '.')
            self.git(repo, 'commit', '-qm', sid)
        self.git(repo, 'checkout', '-q', 'master')
        return repo

    def test_ac4_launch_safety(self):
        for config in ('', 'batch_cap=0', 'batch_cap=true', 'batch_cap="bad"', 'batch_cap=16'):
            self.seed(2)
            self.config.write_text('[merge_queue]\n' + config + '\n')
            rc, batch, _, calls, *_ = self.run_case('invalid-cap-' + config)
            self.assertEqual((rc, batch, calls), (3, None, []))
        ids = self.seed(2)
        self.assertEqual(mq.window_run('../invalid', runner=lambda a: self.fail('gate called')), 2)
        with mq._run_lock('proj'):
            self.assertEqual(self.run_case('held-lock')[0], 3)
        with patch.object(mq.window_gate, 'in_flight', return_value=[dict(window='live', specs=ids)]):
            self.assertEqual(self.run_case('live-reservation')[0], 3)
        self.emit('queue-add', 'OTHER', project='other', branch='other', rule='operator')
        # Re-add our rows so the different project becomes the global head.
        self.stamp += timedelta(seconds=1)
        for sid in ids:
            self.emit('queue-add', sid, project='proj', branch=sid, rule='operator')
        self.assertEqual(self.run_case('global-other-head')[0], 3)
        self.seed(2)
        with patch.object(mq, 'merge_freeze', return_value='unowned nightly red: guard'):
            self.assertEqual(self.run_case('nightly-freeze')[0], 3)
        for mode in ('current-red', 'equal-red', 'old-red', 'returned-red', 'returned-conflict', 'stale-result', 'zero-ready'):
            ids = self.seed(2)
            checked = None
            if mode in ('current-red', 'equal-red', 'zero-ready'):
                if mode == 'equal-red': self.stamp -= timedelta(seconds=10)
                for sid in ids if mode == 'zero-ready' else ids[:1]:
                    self.emit('precheck-red', sid, project='proj', nodes=['guard'])
            elif mode == 'old-red':
                self.emit('precheck-red', ids[0], project='proj', nodes=['guard'])
                self.stamp += timedelta(seconds=1)
                self.emit('queue-add', ids[0], project='proj', branch=ids[0], rule='operator')
            else:
                checked = dict(ts=(self.now - timedelta(seconds=20) if mode == 'stale-result' else self.now).isoformat(),
                               clean=[], **{'conflict_master' if mode == 'returned-conflict' else 'guard_red': {ids[0]: ['guard']}})
            rc, batch, _, calls, out, _ = self.run_case(mode, checked=checked)
            if mode == 'zero-ready':
                self.assertEqual((rc, batch, calls), (3, None, []))
                self.assertIn('precheck-red', out)
                self.assertIn(ids[0], out)
            else:
                self.assertEqual(batch['taken'], [ids[1], ids[0]] if mode == 'old-red' else
                                 ids if mode == 'stale-result' else ids[1:])
        for code in (0, 1, 2, 3):
            ids = self.seed(2)
            self.assertEqual(self.run_case('runner-code-' + str(code), code=code)[0], code)
        ids = self.seed(2)
        def outcome(argv): self.emit('shipped', ids[0], sha='fixture')
        _, _, rows, *_ = self.run_case('sync-reconcile', before=outcome)
        self.assertEqual([r['spec'] for r in mq.queue(rows)], ids[1:])
        ids = self.seed(2)
        rc, batch, *_ = self.run_case('detached-nonblocking', detached=True)
        self.assertEqual(rc, 0)
        self.assertEqual(int((self.root / 'state' / f"window-run.{batch['subject']}.pid").read_text()), os.getpid())
        ids = self.seed(17)
        repo = self.make_repo(ids)
        branch_count = len(self.git(repo, 'show-ref', '--verify', 'refs/heads/' + ids[0]).splitlines())
        self.assertEqual(branch_count, 1)
        self.git(repo, 'branch', '-D', ids[0])
        self.reports.append(dict(case='scratch-branch-deletion-count', count=branch_count))
        rc, batch, rows, calls, _, err = self.run_case('deleted-branch-refusal', real=True)
        self.assertEqual((rc, batch, calls), (3, None, []))
        self.assertIn(ids[0], err)
        refusal = [e for e in rows if e['type'] == 'window-refused'][-1]
        self.assertEqual(refusal['specs'], ids[:1])
        self.assertIn('doit queue remove', refusal['recovery'])
        self.assertIn('operator', refusal['recovery'].lower())
        self.assertEqual(len(mq.queue(rows)), 17)
        self.assertEqual(mq.main(['queue', 'remove', ids[0], '--why', 'deleted scratch branch']), 0)
        rc, batch, rows, *_ = self.run_case('deleted-branch-recovered', real=True)
        self.assertEqual((rc, batch['taken']), (0, ids[1:16]))
        ids = self.seed(17)
        # Re-use the real repository after repairing its deleted branch.
        self.git(repo, 'branch', ids[0], 'master')
        parked_repo = self.root / 'parked-repository'
        repo.rename(parked_repo)
        rc, batch, rows, calls, *_ = self.run_case('missing-repository', real=True)
        self.assertEqual((rc, batch, calls), (3, None, []))
        self.assertEqual([e for e in rows if e['type'] == 'window-refused'][-1]['reason'], 'repository-undetermined')
        parked_repo.rename(repo)
        self.assertEqual(self.run_case('repository-repaired', real=True)[1]['taken'], ids[:15])
        self.seed(2)
        with patch.object(mq, '_train_emit', side_effect=ValueError('persistence fixture')):
            rc, _, _, calls, _, err = self.run_case('refusal-persistence-fails', repository=False)
        self.assertEqual((rc, calls), (3, []))
        self.assertIn('persist', err)
        self.report('AC4')


if __name__ == '__main__':
    unittest.main()
