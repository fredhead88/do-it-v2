#!/usr/bin/env python3
"""Independent scratch round trips for grader refusal recovery.

Every session starts a new interpreter. Packet generation, append/fold, checker
publication, flock, selection and retry accounting use their production paths.
Only transport, preparation and the paid boundary are isolated.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SRC = Path(__file__).resolve().parent
DOIT = SRC.parent / 'doit'
SID = 'L-spec-7901'

# Executed with the scratch environment before importing any product module.
SESSION = r'''
import json, os, pathlib, subprocess, sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
import attempts, autodispatch, build_runner, dispatch, fold, grader_packet, lane_rules, nextstep, spawns, tick
root = pathlib.Path(os.environ['DOIT_ROOT'])
m = json.loads((root / 'fixture.json').read_text())
sid, wt = m['sid'], root / 'worktrees/demo' / m['sid'].lower()
clock = datetime.fromisoformat(m['clock'])
def emit(kind, actor='operator', at=None, **fields):
    path = root / 'events' / ('L-' + actor + '-local.jsonl')
    base = dict(subject=sid, project='demo')
    with patch.object(dispatch, 'now', return_value=at or clock.isoformat()):
        error = dispatch.emit(path, base, kind, **fields)
    assert error is None, error

def snapshot():
    evs = fold.read_events(use_cache=False)
    with patch.object(fold, 'NOW', clock):
        specs = fold.fold(evs)[0]
    return evs, specs

def steps():
    evs, specs = snapshot()
    return nextstep.next_steps(evs, specs, clock, lane_rules.load())

def runner(argv):
    if argv[:2] == ['doit', 'packet']:
        # This is exactly lane_driver._dispatch's argv, through the real CLI.
        result = subprocess.run([str(pathlib.Path(sys.argv[1]).parent / 'doit'), *argv[1:]],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        packet = pathlib.Path(result.stdout.strip())
        assert packet.is_file()
        with (root / 'packet-argv.jsonl').open('a') as f:
            f.write(json.dumps(argv) + '\n')
        return dict(code=0, stdout=str(packet), stderr='')
    assert argv[:3] == ['doit', 'dispatch', '--detach'], argv
    with (root / 'deferred.jsonl').open('a') as f:
        f.write(json.dumps(argv) + '\n')
    return dict(code=0, stdout=json.dumps(dict(detached=os.getpid())), stderr='')

def ordinary(dry_run=False, legacy=True):
    evs, specs = snapshot()
    selected = tick._steps(evs, specs, clock)
    real_popen = subprocess.Popen
    def checker(argv, **kwargs):
        if argv[:2] != ['doit', 'run-checks']:
            return real_popen(argv, **kwargs)
        with (root / 'checker-argv.jsonl').open('a') as output:
            output.write(json.dumps(argv) + '\n')
        return real_popen([sys.executable, '-c', 'pass'], **kwargs)
    with patch.object(dispatch, 'now', return_value=clock.isoformat()), \
         patch.object(tick.subprocess, 'Popen', side_effect=checker):
        out = tick.run_steps(evs, specs, clock, dispatcher=runner, dry_run=dry_run, steps=selected)
    if legacy:
        out += autodispatch.redispatch_dead(evs, specs, clock, runner=runner,
                     dry_run=dry_run, handled=tick.handled_items(selected))
    assert not any(line.startswith('failed ') for line in out), out
    return out

def deferred(mode='silent'):
    argv = json.loads((root / 'deferred.jsonl').read_text().splitlines()[-1])
    packet = pathlib.Path(argv[argv.index('--packet') + 1]).read_text()
    ledger = dispatch.alloc(root / 'events', 'L-grader-', '.jsonl')
    base = dict(subject=sid, project='demo', spawn=ledger.stem)
    def record(kind, **kw):
        with patch.object(dispatch, 'now', return_value=(clock+timedelta(microseconds=1)).isoformat()):
            error = dispatch.emit(ledger, base, kind, **kw)
        assert error is None, error
    class Refused(Exception):
        pass
    def fail(why, reason=None):
        record('spawn-failed', why=why, reason=reason)
        raise Refused(reason)
    evs, _ = snapshot()
    lock = None
    try:
        lock = spawns.gate(root, 'grader', sid, str(wt), evs, fail)
        def local_checker(checker_argv, **kw):
            assert checker_argv[1:] == ['run-checks', sid, '--sha', m['sha']], checker_argv
            with (root / 'checker-argv.jsonl').open('a') as f:
                f.write(json.dumps(checker_argv) + '\n')
            # The launch is real and offline; a silent child publishes nothing.
            return subprocess.Popen([sys.executable, '-c', 'pass'], **kw)
        real_popen = subprocess.Popen
        def launch_checker(checker_argv, **kw):
            if 'run-checks' not in checker_argv:
                return real_popen(checker_argv, **kw)
            with patch.object(subprocess, 'Popen', real_popen):
                child = local_checker(checker_argv, **kw)
                assert child.wait(timeout=10) == 0
                return child
        with patch.object(grader_packet.subprocess, 'Popen', side_effect=launch_checker):
            bundle = grader_packet.gate(evs, sid, packet, record, fail)
        assert bundle is not None
        # Captured seat/model boundary; no backend is invoked.
        with (root / 'paid-entries.jsonl').open('a') as f:
            f.write(json.dumps(dict(packet=argv[argv.index('--packet') + 1],
                                   digest=bundle['event']['bundle_sha256'])) + '\n')
        record('spawn-started', role='grader', worktree=str(wt), pid=os.getpid())
        return 'entered'
    except Refused as exc:
        return str(exc)
    finally:
        if lock is not None:
            lock.close()

def publish():
    # Real run(), clone/check execution, atomic publication, subprocess append.
    aliases, backing = root.parent / 'aliases', root.parent / 'backing'
    aliases.mkdir(exist_ok=True)
    backing.mkdir(exist_ok=True)
    with patch.object(build_runner, 'SHORT_TMP_ROOT', aliases), \
         patch.object(build_runner, 'SHORT_TMP_BACKING_ROOT', backing):
        return str(build_runner.run(sid, m['sha']))
'''


class RetryRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rt-', dir='/var/tmp')
        self.addCleanup(self.temp.cleanup)
        self.top = Path(self.temp.name)
        self.root = self.top / 'root'
        self.repo = self.root / 'repos/demo'
        self.repo.mkdir(parents=True)
        for name in ('events', 'content', 'packets', 'worktrees/demo/' + SID.lower()):
            (self.root / name).mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items()
                    if not (k.startswith('SUPABASE_DB_URL') or 'DSN' in k or 'DATABASE' in k
                            or k in ('PYTHONPATH', 'AS_LOCAL_PG_ADMIN', 'DOIT_SEAT', 'DOIT_SUPERVISED',
                                     'DOIT_PROJECT'))}
        # Tick markers carry no project: ordinary orchestration must read the
        # whole isolated ledger, including the parent side of detached pairs.
        self.env.update(DOIT_ROOT=str(self.root), DOIT_SCRATCH=str(self.top / 'scratch'),
                        DOIT_NO_POKE='1', DOIT_MAIN='fixture',
                        DOIT_LEDGER_FILE='L-operator-local.jsonl',
                        DOIT_GRADING_TOML=str(self.root / 'grading.toml'))
        (self.root / 'grading.toml').write_text('[project.demo]\nrunner_wall_cap_s = 20\n')
        self.git('init', '-q', '-b', 'fixture')
        self.git('config', 'user.name', 'fixture')
        self.git('config', 'user.email', 'fixture@localhost')
        (self.repo / 'probe.py').write_text("print('offline checker passed')\n")
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture base')
        base = self.git('rev-parse', 'HEAD')
        (self.repo / 'change').write_text('fixture build\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture build')
        sha = self.git('rev-parse', 'HEAD')
        self.clock = datetime.now(timezone.utc).replace(microsecond=0)
        self.meta = dict(sid=SID, base=base, sha=sha, clock=self.clock.isoformat())
        self.save()
        spec = self.root / 'content' / (SID + '.md')
        spec.write_text('## Acceptance criteria\nAC1 [backend]: offline fixture.\n'
                        'check: data_source=scratch customer_data=false profile=repo-venv '
                        'timing=pre-merge answered_by=script\n'
                        'review_path: run `/usr/bin/python3 probe.py`\n'
                        '## Verification\n```bash\n/usr/bin/python3 probe.py\n```\n')
        card = self.root / 'content/card-fixture.md'
        card.write_text('# Scratch output card\nAC1 [backend] done\nverify exit=0\n')
        self.session("emit('spec-written', 'spec-writer', at=(clock-timedelta(days=4)).isoformat(), "
                     "path=str(root/'content'/ (sid+'.md')), footprint=['probe.py']); "
                     "emit('spawn-done', 'spec-auditor', at=(clock-timedelta(days=3)).isoformat(), bad_cut=False); "
                     "emit('build-done', 'builder', at=(clock-timedelta(days=2)).isoformat(), "
                     "status='DONE', ready_sha=m['sha'], base_sha=m['base'], card=str(root/'content/card-fixture.md')); "
                     "result=True")

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args],
                                       env=self.env, text=True, stderr=subprocess.DEVNULL).strip()

    def save(self):
        (self.root / 'fixture.json').write_text(json.dumps(self.meta))

    def at(self, instant):
        self.clock = instant
        self.meta['clock'] = instant.isoformat()
        self.save()

    def session(self, body):
        command = SESSION + '\n' + body + '\nprint("RESULT:" + json.dumps(result, default=str))\n'
        p = subprocess.run([sys.executable, '-c', command, str(SRC)], env=self.env,
                           capture_output=True, text=True, timeout=45)
        self.assertEqual(p.returncode, 0, p.stderr + '\n' + p.stdout)
        return json.loads(next(l[7:] for l in reversed(p.stdout.splitlines()) if l.startswith('RESULT:')))

    def rows(self, name):
        p = self.root / name
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def signed(self, ac, observations):
        body = json.dumps(observations, sort_keys=True)
        print(json.dumps(dict(url='cli://retry1851/' + ac, status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_retry1851_AC3_committed_bundle_round_trip_and_stale_packet(self):
        self.session('result=ordinary()')
        self.assertEqual(len(self.rows('checker-argv.jsonl')), 1)
        self.assertEqual(self.rows('deferred.jsonl'), [])
        self.assertEqual(self.rows('paid-entries.jsonl'), [])
        published = self.session('result=publish()')
        count = len(self.session("result=[e for e in snapshot()[0] if e['type']=='evidence-bundle']"))
        self.assertEqual(self.session('result=publish()'), published)
        self.assertEqual(len(self.session("result=[e for e in snapshot()[0] if e['type']=='evidence-bundle']")), count)
        self.at(datetime.now(timezone.utc))
        self.session('result=ordinary()')
        argv = self.rows('deferred.jsonl')[-1]
        packet_path = Path(argv[argv.index('--packet')+1])
        original = packet_path.read_text()
        self.assertIn('bundle_sha256:', original)
        self.assertIn('untrusted evidence', original)
        self.assertNotIn('L-builder-', original)
        packet_path.write_text('stale packet without digest')
        self.assertEqual(self.session('result=deferred()'), 'bundle-pending')
        self.assertEqual(len(self.rows('checker-argv.jsonl')), 1)
        packet_path.write_text(original)
        self.assertEqual(self.session('result=deferred()'), 'entered')
        self.assertEqual(len(self.rows('paid-entries.jsonl')), 1)
        self.session('result=ordinary()')
        self.assertEqual(len(self.rows('paid-entries.jsonl')), 1)
        self.signed('AC3-publication', dict(pending_entries=0, ready_entries=1,
                    reopened_digest=True, receiver_launches=0, existing_path_no_event=True))

    def test_retry1851_AC3_real_flock_and_silent_checker(self):
        self.session('result=publish()')
        self.at(datetime.now(timezone.utc))
        self.session('result=ordinary()')
        sys.path.insert(0, str(SRC))
        import spawns
        lock, refusal = spawns.acquire(self.root, 'grader', SID,
                                        str(self.root / 'worktrees/demo' / SID.lower()), [])
        self.assertIsNone(refusal)
        try:
            self.assertEqual(self.session('result=deferred()'), 'dispatch-in-progress')
            self.at(self.clock+timedelta(seconds=1))
            self.session('result=ordinary()')
            self.assertEqual(self.session('result=deferred()'), 'dispatch-in-progress')
            self.assertEqual(self.rows('checker-argv.jsonl'), [])
        finally:
            lock.close()
        # Removing evidence leaves the receiving gate pending without a launch.
        bundle_path = Path(self.session("result=next(e['path'] for e in reversed(snapshot()[0]) if e['type']=='evidence-bundle')"))
        bundle_path.unlink()
        self.assertEqual(self.session('result=deferred()'), 'bundle-pending')
        self.assertEqual(self.rows('checker-argv.jsonl'), [])
        self.at(self.clock+timedelta(seconds=1))
        self.session('result=ordinary()')
        self.assertEqual(len(self.rows('checker-argv.jsonl')), 1)
        self.session('result=ordinary()')  # close the vanished silent checker once
        step = self.session('result=next(s.__dict__ for s in steps() if s.item==sid)')
        self.assertIn('retry-wait: bundle', step['reason'])
        due = datetime.fromisoformat(step['retry_at'])
        self.at(due-timedelta(microseconds=1))
        self.session('result=ordinary()')
        self.assertEqual(len(self.rows('checker-argv.jsonl')), 1)
        self.at(due)
        self.session('result=ordinary()')
        self.assertEqual(len(self.rows('checker-argv.jsonl')), 2)
        self.assertEqual(self.rows('paid-entries.jsonl'), [])
        self.signed('AC3-flock-silent', dict(lock_refusals=2, checker_requests=2, paid_entries=0,
                                           boundary_seconds=300, receiver_launches=0))

    def test_retry1851_AC3_actual_dispatch_cannot_open_paths(self):
        self.session('result=publish()')
        self.at(datetime.now(timezone.utc))
        self.session('result=ordinary()')
        for number, message, code in ((13, 'Permission denied', 1), (28, 'No space left on device', 2)):
            self.at(self.clock + timedelta(seconds=number))
            body = """
import builtins, errno
argv = json.loads((root/'deferred.jsonl').read_text().splitlines()[-1])
a = SimpleNamespace(role='grader', subject=sid, packet=argv[argv.index('--packet')+1],
                    cwd=str(wt), project='demo', charter=None, seat=False)
original = builtins.open
lock_path = root/'state'/spawns.lock_name(sid)
def cannot_open(path, *args, **kw):
    if pathlib.Path(path) == lock_path:
        raise OSError(NUMBER, MESSAGE)
    return original(path, *args, **kw)
try:
    with patch.object(builtins, 'open', side_effect=cannot_open), \\
         patch.object(dispatch, 'now', return_value=clock.isoformat()):
        dispatch.main(a)
except SystemExit as exc:
    result=dict(code=exc.code, rows=snapshot()[0])
else:
    raise AssertionError('dispatch passed forced lock-open failure')
""".replace('NUMBER', str(number)).replace('MESSAGE', repr(message))
            result = self.session(body)
            self.assertEqual(result['code'], code)
            latest = result['rows'][-1]
            self.assertEqual(latest['type'], 'spawn-failed' if number == 13 else 'infra-failure')
            self.assertIn(message, latest.get('why', latest.get('detail', '')))
            if number == 13:
                self.assertEqual(latest['reason'], 'dispatch-in-progress')
            else:
                self.assertFalse(any(e['type'] == 'spawn-failed' and e.get('spawn') == latest['spawn']
                                     for e in result['rows']))
        self.assertEqual(self.rows('paid-entries.jsonl'), [])
        self.signed('AC3-cannot-open', dict(permission_exit=1, enospc_exit=2,
                    permission_event='spawn-failed', enospc_event='infra-failure', paid_entries=0))

    def test_retry1851_AC4_aged_recovery_and_legacy_single_launch(self):
        # Existing deployed histories: no migration and no two-hour cutoff.
        for action in ('grade', 'regrade'):
            for reason in ('bundle-pending', 'duplicate-dispatch', 'dispatch-in-progress'):
                self.setUp()
                self.session('result=publish()')
                self.at(datetime.now(timezone.utc))
                body = """
if ACTION == 'regrade':
    emit('decision', at=(clock-timedelta(days=1, minutes=1)).isoformat(), regrade='yes')
emit('autodispatched', 'tick', at=(clock-timedelta(days=1)).isoformat(), role='grader', spawn='45678', since=(clock-timedelta(days=2)).isoformat())
emit('spawn-failed', 'grader', at=(clock-timedelta(days=1)+timedelta(microseconds=1)).isoformat(), spawn='L-grader-7800',
     reason=REASON, why='another dispatch holds the subject lock')
result=ordinary()
""".replace('ACTION', repr(action)).replace('REASON', repr(reason))
                # New governing trigger isolates each historical case.
                self.session("emit('build-done', 'builder', at=(clock-timedelta(days=2)).isoformat(), "
                             "status='DONE', ready_sha=m['sha'], base_sha=m['base'], "
                             "card=str(root/'content/card-fixture.md')); result=True")
                before = len(self.rows('deferred.jsonl'))
                result = self.session(body)
                self.assertEqual(len(self.rows('deferred.jsonl')), before + 1, result)
                self.assertIn(' ' + action + ' ', result[0])
                # Close the successful transport marker with a genuine verdict,
                # then the next case's new build opens a fresh grading cycle.
                self.session("emit('verdict', 'grader', confirmed=True); result=True")
        self.signed('AC4-aged', dict(cases=6, launches=6, ordinary_and_legacy=True, age_days=1))

    def test_retry1851_AC4_visible_conflict_and_protected_holds(self):
        self.session('result=publish()')
        self.at(datetime.now(timezone.utc))
        self.session("emit('spawn-failed', 'grader', spawn='L-grader-7801', reason='duplicate-dispatch'); result=True")
        self.session("emit('spawn-started', 'builder', role='builder', spawn='L-builder-7801', worktree=str(wt)); result=True")
        result = self.session('result=ordinary()')
        self.assertTrue(any('grade-wait: conflict' in line for line in result), result)
        self.assertEqual(self.rows('deferred.jsonl'), [])
        self.session("emit('spawn-done', 'builder', spawn='L-builder-7801'); result=True")
        self.session('result=ordinary(dry_run=True)')
        self.assertEqual(self.rows('deferred.jsonl'), [])
        self.session('result=ordinary()')
        self.assertEqual(len(self.rows('deferred.jsonl')), 1)
        self.signed('AC4-conflict', dict(open_launches=0, dry_run_launches=0, terminal_launches=1))

    def test_retry1851_AC4_preparation_retry_requires_due_or_new_trigger(self):
        for new_build in (False, True):
            with self.subTest(new_build=new_build):
                if new_build:
                    self.setUp()
                self.session('result=publish()')
                self.at(datetime.now(timezone.utc))
                self.session("emit('autodispatch-failed', 'tick', role='grader', rule='grade', "
                             "at=(clock-timedelta(minutes=4)).isoformat(), "
                             "reason='fixture preparation failure'); result=ordinary()")
                self.assertEqual(self.rows('deferred.jsonl'), [])
                if new_build:
                    self.session("emit('build-done', 'builder', status='DONE', ready_sha=m['sha'], "
                                 "base_sha=m['base'], card=str(root/'content/card-fixture.md')); result=True")
                    self.session('result=ordinary()')
                    self.assertEqual(self.rows('deferred.jsonl'), [])
                    self.session('result=publish()')
                    self.at(datetime.now(timezone.utc))
                else:
                    self.at(self.clock + timedelta(minutes=1))
                self.session('result=ordinary()')
                self.assertEqual(len(self.rows('deferred.jsonl')), 1)
                self.assertEqual(self.session('result=deferred()'), 'entered')
                self.assertEqual(len(self.rows('paid-entries.jsonl')), 1)
        self.signed('AC4-preparation', dict(early_launches=0, due_launches=1,
                    new_build_launches=1, captured_entries=2))


if __name__ == '__main__':
    unittest.main()
