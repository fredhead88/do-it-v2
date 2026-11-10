#!/usr/bin/env python3
"""Hermetic activation/restore checks: every runtime root is temporary."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import tg_cutover as cut


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='telegram-cutover-')
        self.addCleanup(self.tmp.cleanup)
        # Runner-owned TMPDIR may have a symlinked ancestor. Canonicalize the
        # allocated scratch root, keeping runtime parent-link checks strict.
        self.base = Path(self.tmp.name).resolve(strict=True)
        self.root, self.push, self.repo = [self.base / n for n in ('root', 'push', 'repo')]
        for p in (self.root, self.push, self.repo):
            p.mkdir()
        self.events = []
        self.cron = '# keep\nMAILTO=""\n*/5 * * * * python3 /old/thinker-inbox.py\n* * * * * python3 /old/tg-gate.py\n0 9 * * * python3 /old/tg-daily.py\n'
        self.original_cron = self.cron
        self.loop = b'#!/bin/bash\nlog(){ echo "$*" >> "$LOG"; }\ntg(){ /old/tg-send.sh "$*"; log "TG $*"; }\n# preserve loop work\n'
        files = {
            'push/rloop.sh': self.loop,
            'push/rloop.run.sh': self.loop,
            'push/restart-driver.sh': b'#!/bin/sh\n# fixture OS boundary\n',
            'push/foreman/catcher/catcher.py': b'import subprocess\nTG="/old/tg-send.sh"\nsubprocess.run([TG, "outage"], capture_output=True)\nsubprocess.run([TG, f"daily {1}"], capture_output=True)\n',
            'push/hetzner/watch.py': b'import subprocess\nTG="/old/tg-send.sh"\n# provisioning preserved\nsubprocess.run([TG, f"ready {1}"])\n',
            'root/bin/swap-watch.sh': b'#!/bin/sh\nfind "$TMPDIR" -name cutover-junk -delete\n    touch "$TMPDIR/cleaned"; [ "100" -lt 2048 ] && /old/tg-send.sh "alert" >/dev/null 2>&1 || true\n',
        }
        for key in cut.TARGETS:
            files.setdefault(key, b'original\n')
        files.pop('root/state/tg-cutover-done', None)
        for key, data in files.items():
            prefix, rel = key.split('/', 1)
            p = {'root': self.root, 'push': self.push}[prefix] / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            p.chmod(0o750)
        for source in cut.LINKS.values():
            p = self.repo / source
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('# source '+source+'\n')
        (self.repo / 'doit').write_text('# dispatcher\n')
        self.rows = [dict(name=n, where='user', schedule='*/5 * * * *', command=c)
                     for n, c in [('thinker-inbox', 'python3 '+str(self.repo/'src/thinker_inbox.py')),
                                  ('merge-news', str(self.repo/'doit')+' merge-news flush'),
                                  ('telegram-daily', 'python3 '+str(self.repo/'src/daily_summary.py'))]]
        self.driver = True
        self.helper_codes = []
        self.gate_code = 0
        self.clock = 1000.
        self.pid = 10
        self.process = {'pid': 10, 'generation': 1, 'started': 900., 'source': str(self.root/'telegram/tg-poll.py'), 'identity': 'old'}
        self.mode = ''
        self.c = cut.Cutover(self.root, self.push, self.repo, self)
        self.original = {key: self.c.snapshot(self.c.path(key)) for key in cut.TARGETS}

    def cron_read(self):
        return self.cron

    def cron_write(self, text):
        self.events.append('cron')
        if self.mode == 'cron-fail' and 'tg-daily.py' not in text:
            raise OSError('fixture')
        self.cron = text

    def helper(self, push):
        self.events.append('helper')
        (push/'rq').mkdir(exist_ok=True)
        (push/'rq/stop').touch()
        code = self.helper_codes.pop(0) if self.helper_codes else 0
        if not code:
            if self.mode != 'false-copy':
                (push/'rloop.run.sh').write_bytes((push/'rloop.sh').read_bytes())
            if self.mode != 'false-driver':
                self.driver = True
        return code

    def driver_running(self, push):
        self.events.append('driver-check')
        return self.driver

    def gate(self, repo):
        self.events.append('tg-check')
        print('ordinary tg-check diagnostic: '+('oasdiff/Lambda open' if self.gate_code else 'accounted'))
        return self.gate_code

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds
        if self.process is None and self.mode != 'no-start':
            self.pid += 1
            source, identity = cut.source_identity(self.root/'telegram/tg-poll.py')
            self.process = {'pid': self.pid, 'generation': self.pid, 'started': self.clock,
                            'source': source if self.mode != 'wrong-source' else '/wrong/file.py', 'identity': identity}
            self.events.append('keepalive-start')

    def pollers(self, path):
        if self.mode == 'ambiguous':
            raise cut.Failure('poller ownership ambiguous')
        return [] if self.process is None else [dict(self.process)]

    def stop(self, proc):
        self.events.append('stop:'+str(proc['pid']))
        if self.mode == 'stop-fail':
            raise cut.Failure('poller stop failed')
        if self.mode != 'stale':
            self.process = None

    def manifest(self, repo):
        return self.rows

    def apply(self, revert=False, dry=False):
        return self.c.run(dry_run=dry, revert=revert)

    def assert_stopgaps(self):
        self.assertIn('tg-gate.py', self.cron)
        self.assertIn('tg-daily.py', self.cron)
        self.assertFalse((self.root/'state/tg-cutover-done').exists())

    def signed(self, criterion, cases):
        body = json.dumps(cases, sort_keys=True)
        print(json.dumps(dict(url='cli://telegram-cutover/'+criterion, status=0,
                             body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=cases)))


class CutoverOrder(Fixture):
    def test_success(self):
        self.assertEqual(self.apply(), 0)
        self.assertLess(self.events.index('helper'), self.events.index('stop:10'))
        self.assertLess(self.events.index('keepalive-start'), self.events.index('tg-check'))
        self.assertNotIn('tg-daily.py', self.cron)
        self.assertTrue((self.root/'state/tg-cutover-done').is_file())
        self.assertEqual((self.push/'rloop.run.sh').read_bytes(), (self.push/'rloop.sh').read_bytes())
        self.signed('AC2', self.events)

    def test_refusal_bounded_recovery(self):
        for running in (True, False):
            with self.subTest(running=running):
                self.driver = running
                self.events.clear()
                self.helper_codes = [8]
                self.assertNotEqual(self.apply(), 0)
                self.assertEqual((self.push/'rloop.sh').read_bytes(), self.loop)
                self.assertFalse((self.push/'rq/stop').exists())
                self.assertEqual(self.events.count('helper'), 1 if running else 2)
                self.assertNotIn('cron', self.events)
                self.assert_stopgaps()
        self.signed('AC2-refusal', ['surviving driver', 'absent driver: one recovery'])


class CutoverCLI(Fixture):
    def tree(self):
        """Every path under the scratch base: kind, bytes, mode, symlink text."""
        seen = {}
        for p in sorted(self.base.rglob('*')):
            rel = str(p.relative_to(self.base))
            if p.is_symlink():
                seen[rel] = ('link', os.readlink(p))
            elif p.is_dir():
                seen[rel] = ('dir', p.stat().st_mode & 0o7777)
            else:
                seen[rel] = ('file', p.read_bytes(), p.stat().st_mode & 0o7777)
        return seen

    def dry(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = self.apply(dry=True)
        return code, out.getvalue()

    def test_dry_and_invalid_flags(self):
        self.gate_code = 7
        # A pre-existing link in the tree, so symlink text is compared too.
        (self.base/'sentinel-link').symlink_to('/nonexistent/target-text')
        before, cron_before = self.tree(), self.cron
        code, printed = self.dry()
        self.assertEqual(code, 0)
        self.assertEqual(self.events, ['tg-check'])
        self.assertFalse(self.c.state.exists())
        self.assertEqual(self.cron, self.original_cron)
        self.assertEqual(self.cron, cron_before)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.tree()['sentinel-link'], ('link', '/nonexistent/target-text'))
        # Printed inventory: every target, the plan, and the nonzero gate status.
        for key in cut.TARGETS:
            self.assertIn('Plan target: '+key, printed)
        self.assertIn('Plan: durable backup; atomic loop source; unchanged helper', printed)
        self.assertIn('ordinary tg-check diagnostic: oasdiff/Lambda open', printed)
        self.assertIn('Current sender gate status: 7; dry-run makes no changes.', printed)
        # Missing planning input: fails, and still changes nothing.
        missing = self.repo/next(iter(cut.LINKS.values()))
        data = missing.read_bytes()
        missing.unlink()
        before = self.tree()
        self.events.clear()
        code, printed = self.dry()
        self.assertEqual(code, 1)
        self.assertNotIn('tg-check', self.events)
        self.assertNotIn('Plan target:', printed)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.cron, cron_before)
        missing.write_bytes(data)
        missing.chmod(0o640)
        # Unreadable planning input (skipped when permissions do not bind, e.g. root).
        target = self.c.path('push/hetzner/watch.py')
        target.chmod(0)
        try:
            if not os.access(target, os.R_OK):
                before = self.tree_unreadable_safe()
                self.events.clear()
                code, printed = self.dry()
                self.assertEqual(code, 1)
                self.assertNotIn('tg-check', self.events)
                self.assertEqual(self.tree_unreadable_safe(), before)
                self.assertEqual(self.cron, cron_before)
        finally:
            target.chmod(0o750)
        # Subprocess parser coverage never opens real runtime roots: a throwaway
        # DOIT_ROOT and an unrelated cwd, with the tree compared afterwards.
        before = self.tree()
        env = {**os.environ, 'DOIT_ROOT': str(self.base/'subprocess-root')}
        doit = str(Path(__file__).resolve().parents[1]/'doit')
        for flags in (['--dry-run', '--revert'], ['--bogus']):
            result = subprocess.run([doit, 'telegram-cutover', *flags], capture_output=True, text=True,
                                    cwd='/', env=env)
            self.assertEqual(result.returncode, 2, flags)
        self.assertFalse((self.base/'subprocess-root').exists())
        self.assertEqual(self.tree(), before)
        self.signed('AC1', ['dry-run: bytes, modes, link text and cron unchanged; gate status 7 displayed; inventory printed',
                            'missing input: 1, no writes', 'mutually exclusive flags: 2'])

    def tree_unreadable_safe(self):
        """tree() with unreadable files recorded by mode only."""
        seen = {}
        for p in sorted(self.base.rglob('*')):
            rel = str(p.relative_to(self.base))
            if p.is_symlink():
                seen[rel] = ('link', os.readlink(p))
            elif p.is_dir():
                seen[rel] = ('dir', p.stat().st_mode & 0o7777)
            else:
                mode = p.stat().st_mode & 0o7777
                seen[rel] = ('file', p.read_bytes() if os.access(p, os.R_OK) else None, mode)
        return seen


class CutoverTargets(Fixture):
    def test_closed_inventory_and_fact_data(self):
        originals = {p: (self.repo/p).read_bytes() for p in cut.LINKS.values()}
        self.assertEqual(self.apply(), 0)
        for key, source in cut.LINKS.items():
            self.assertTrue(self.c.path(key).is_symlink(), key)
            self.assertEqual(self.c.path(key).resolve(), self.repo/source)
        for key in cut.LEGACY:
            self.assertFalse(self.c.path(key).exists())
            self.assertEqual(self.c.record['images'][key], self.original[key])
        self.assertEqual(originals, {p: (self.repo/p).read_bytes() for p in cut.LINKS.values()})
        # Execute only the patched tg function with a harmless fixture dispatcher.
        # The actual sender and merge-news implementations are never invoked.
        arguments = self.base/'args.json'
        dispatcher = self.repo/'doit'
        dispatcher.write_text('#!/usr/bin/python3\nimport json,sys,os\nfrom pathlib import Path\nassert Path(os.environ["LOG"]).read_text().startswith("TG ")\nPath(os.environ["ARGS"]).write_text(json.dumps(sys.argv[1:]))\n')
        dispatcher.chmod(0o700)
        payload = 'quotes " \' $HOME ; $(touch '+str(self.base/'injected')+')\nnext line'
        log = self.base/'loop.log'
        subprocess.run(['bash', '-c', 'source "$1"; tg "$2"', 'fixture', str(self.push/'rloop.sh'), payload],
                       env={**os.environ, 'LOG': str(log), 'ARGS': str(arguments)}, check=True)
        self.assertEqual(json.loads(arguments.read_text()), ['merge-news', 'record', payload])
        self.assertFalse((self.base/'injected').exists())
        self.assertIn('# preserve loop work', (self.push/'rloop.sh').read_text())
        junk = self.base/'cutover-junk'
        junk.touch()
        subprocess.run(['sh', str(self.root/'bin/swap-watch.sh')], env={**os.environ, 'TMPDIR': str(self.base)}, check=True)
        self.assertFalse(junk.exists())
        self.assertTrue((self.base/'cleaned').exists())
        for key, labels, sender in [('push/foreman/catcher/catcher.py', ['[DOWN]', '[DAILY]'], 'catcher'), ('push/hetzner/watch.py', ['[DAILY]'], 'hetzner-watch')]:
            text = self.c.path(key).read_text()
            from notification_policy import LABELS
            for label in labels:
                self.assertIn(label, LABELS)
                self.assertIn(repr(label), text)
            self.assertIn(repr(sender), text)
            self.assertIn("'tg-say'", text)
        self.signed('AC3', list(cut.TARGETS)+['fact argv/log order; cleanup; both catcher sites; unchanged producer bytes'])


class CutoverFailures(Fixture):
    def test_gate_nonzero_restores_transport_only(self):
        self.gate_code = 13
        self.assertEqual(self.apply(), 1)
        self.assert_stopgaps()
        self.assertEqual(self.c.snapshot(self.c.path('root/telegram/tg-send.sh')), self.original['root/telegram/tg-send.sh'])
        self.assertTrue(self.c.path('root/telegram/tg-poll.py').is_symlink())
        self.assertNotEqual((self.push/'rloop.sh').read_bytes(), self.loop)
        self.gate_code = 0
        old_stops = self.events.count('stop:10')
        self.assertEqual(self.apply(), 0)
        self.assertEqual(self.events.count('stop:10'), old_stops)
        self.assertEqual(self.events.count('tg-check'), 2)
        self.signed('AC4-gate', ['status 13; pre-image sender restored by fresh read; other install retained; status 0 retried'])

    def test_failure_matrix(self):
        cases = []
        for mode in ('preimage', 'atomic-loop', 'false-copy', 'false-driver', 'recovery-fail', 'ambiguous-patch', 'missing-source', 'install', 'cron-fail', 'marker-fail'):
            f = Fixture('runTest'); f.setUp()
            try:
                original_atomic = cut.atomic
                failed = [False]
                def inject(path, data, mode_bits=0o600):
                    target = {'preimage': f.c.state/'record.json', 'atomic-loop': f.push/'rloop.sh', 'marker-fail': f.root/'state/tg-cutover-done'}.get(mode)
                    if path == target and not failed[0]:
                        failed[0] = True
                        raise OSError('fixture')
                    return original_atomic(path, data, mode_bits)
                if mode in ('false-copy', 'false-driver', 'cron-fail'):
                    f.mode = mode
                if mode == 'false-driver':
                    f.driver = False
                if mode == 'recovery-fail':
                    f.driver = False; f.helper_codes = [9, 8]
                if mode == 'ambiguous-patch':
                    (f.push/'rloop.sh').write_text('unsupported\n')
                if mode == 'missing-source':
                    (f.repo/'scripts/pulse.py').unlink()
                restore = f.c.restore
                def install(path, image):
                    if mode == 'install' and path == f.root/'telegram/pulse.py':
                        raise OSError('fixture')
                    return restore(path, image)
                with mock.patch.object(cut, 'atomic', side_effect=inject), mock.patch.object(f.c, 'restore', side_effect=install):
                    self.assertEqual(f.apply(), 1, mode)
                f.assert_stopgaps()
                if mode in ('preimage', 'atomic-loop', 'false-copy', 'false-driver', 'recovery-fail', 'ambiguous-patch', 'missing-source'):
                    self.assertEqual(f.c.snapshot(f.root/'telegram/tg-send.sh'), f.original['root/telegram/tg-send.sh'])
                cases.append(mode)
            finally:
                f.doCleanups()
        self.signed('AC4', cases)

    def test_lock_and_interrupted_retirement(self):
        import fcntl
        self.c.state.mkdir(parents=True, mode=0o700)
        with (self.c.state/'lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.apply(), 1)
            self.assertEqual(self.events, [])
        self.assertEqual(self.apply(), 0)
        self.c.record['retiring'] = True
        self.c.save()
        self.gate_code = 11
        self.c = cut.Cutover(self.root, self.push, self.repo, self)
        self.assertEqual(self.apply(), 1)
        self.assert_stopgaps()
        self.signed('AC4-interruption', ['flock blocks interleaving', 'durable retirement intent recovered before retry'])


class CutoverRestore(Fixture):
    def child_action(self, args):
        cron_file = self.base/'cron.txt'
        cron_file.write_text(self.cron)
        (self.base/'rows.json').write_text(json.dumps(self.rows))
        script = '''import sys,json
from pathlib import Path
import tg_cutover as cut
from test_tg_cutover import Fixture
f=Fixture('runTest')
f.root,f.push,f.repo,f.base=map(Path,sys.argv[1:5])
f.events=[]; f.cron=Path(sys.argv[5]).read_text(); f.clock=1000.; f.pid=30
f.rows=json.loads((f.base/'rows.json').read_text())
f.driver=True; f.helper_codes=[]; f.mode=''; f.process=None; f.gate_code=0
cutover=cut.Cutover(f.root,f.push,f.repo,f)
original=cut.Cutover
cut.Cutover=lambda *args: cutover
result=cut.main(sys.argv[6:])
Path(sys.argv[5]).write_text(f.cron)
sys.exit(result)
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.root), str(self.push), str(self.repo), str(self.base), str(cron_file), *args],
                                env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parent)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.cron = cron_file.read_text()

    def child_revert(self):
        self.child_action(['--revert'])

    def test_subprocess_apply(self):
        self.child_action([])
        self.assertTrue((self.root/'state/tg-cutover-done').exists())
        self.child_action(['--dry-run'])
        self.child_revert()
        self.assertEqual(self.cron,self.original_cron)
        self.signed('AC1-subprocess',['main apply: 0','main --dry-run after apply: 0','main --revert: 0'])

    def test_fresh_process_round_trip(self):
        # Missing pre-images and original links are restored as objects.
        (self.root/'telegram/pulse.py').unlink()
        self.original['root/telegram/pulse.py'] = dict(kind='missing')
        outside = self.base/'outside'
        outside.write_text('sentinel')
        (self.root/'telegram/tg-send-file.sh').unlink()
        (self.root/'telegram/tg-send-file.sh').symlink_to(outside)
        self.original['root/telegram/tg-send-file.sh'] = dict(kind='link', target=str(outside))
        self.assertEqual(self.apply(), 0)
        record = (self.c.state/'record.json').read_bytes()
        self.assertEqual(self.apply(), 0)
        self.assertEqual(json.loads(record)['images'], json.loads((self.c.state/'record.json').read_bytes())['images'])
        self.child_revert()
        for key, image in self.original.items():
            self.assertEqual(self.c.snapshot(self.c.path(key)), image, key)
        self.assertEqual(self.cron, self.original_cron)
        self.assertEqual(outside.read_text(), 'sentinel')
        self.assertEqual(self.c.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.c.state/'record.json').stat().st_mode & 0o777, 0o600)
        self.child_revert()
        self.signed('AC5', ['fresh main --revert process: files/modes/links/missing/inbox/crontab/marker', 'repeat apply retains original; repeat revert safe'])

    def test_partial_and_refused_revert(self):
        self.gate_code = 5
        self.assertEqual(self.apply(), 1)
        self.child_revert()
        self.assertEqual(self.cron, self.original_cron)
        self.signed('AC5-partial', ['fresh-process restore after gate failure'])

    def test_refused_revert_matrix(self):
        cases = []
        for mode in ('surviving', 'absent', 'false-copy', 'false-driver', 'recovery-fail'):
            f=Fixture('runTest'); f.setUp()
            try:
                self.assertEqual(f.apply(), 0)
                images={key: f.c.snapshot(f.c.path(key)) for key in cut.TARGETS}
                original=f.c.record['images']
                f.events.clear()
                f.driver=mode not in ('absent','false-driver','recovery-fail')
                f.mode=mode
                if mode in ('surviving','absent'):
                    f.helper_codes=[7]
                elif mode=='recovery-fail':
                    f.helper_codes=[7,9]
                self.assertEqual(f.apply(revert=True),1,mode)
                for key,image in images.items():
                    self.assertEqual(f.c.snapshot(f.c.path(key)),image,key)
                self.assertEqual(f.c.record['images'],original)
                self.assertEqual(f.events.count('helper'),1 if f.driver and mode not in ('absent','recovery-fail','false-driver') else 2)
                cases.append(mode)
            finally:
                f.doCleanups()
        self.signed('AC5-refused',cases)

    def test_absent_corrupt_backup(self):
        self.assertEqual(self.apply(revert=True),1)
        self.assertEqual(self.events,[])
        (self.c.state/'record.json').write_text('{}')
        (self.c.state/'record.json').chmod(0o600)
        self.assertEqual(self.apply(revert=True),1)
        self.assertEqual(self.events,[])
        self.signed('AC5-backup',['absent and corrupt: no live changes'])


class CutoverIntegration(Fixture):
    def test_existing_cron_forms(self):
        cases=[]
        for shape in ('legacy','versioned','mixed'):
            f=Fixture('runTest'); f.setUp()
            try:
                if shape!='legacy':
                    f.cron=f.cron.replace('python3 /old/thinker-inbox.py',f.rows[0]['command']+' # doit-cron:thinker-inbox')
                if shape=='mixed':
                    for row in f.rows[1:]:
                        f.cron+=row['schedule']+' '+row['command']+' # doit-cron:'+row['name']+'\n'
                original=f.cron
                self.assertEqual(f.apply(),0)
                for row in f.rows:
                    self.assertEqual(f.cron.count(row['command']),1)
                self.assertIn('MAILTO=""',f.cron)
                self.assertIn('# keep',f.cron)
                self.assertEqual(f.apply(revert=True),0)
                self.assertEqual(f.cron,original)
                cases.append(shape)
            finally:
                f.doCleanups()
        self.signed('AC6-cron',cases)

    def test_runbooks_and_inherited_manifest(self):
        repo=Path(__file__).resolve().parents[1]
        # Compare parsed executable rows, not a comment-sensitive byte snapshot.
        import tomllib
        base=subprocess.run(['git','show','HEAD:crons.toml'],cwd=repo,check=True,capture_output=True,text=True).stdout
        self.assertEqual(tomllib.loads(base),tomllib.loads((repo/'crons.toml').read_text()))
        for name in ('thinker','executor'):
            text=(repo/'agents'/f'{name}.md').read_text()
            for marker in ('telegram-cutover --dry-run','telegram-cutover --revert','SD23','SD19','SD21','180-second','rloop.run.sh','--list','Q-A'):
                self.assertIn(marker,text)
        self.assertNotIn('unanswered for 60 minutes',(repo/'agents/executor.md').read_text())
        self.signed('AC6',['parsed executable cron rows unchanged','runbooks cover full-wave dry-run, loop, poller, gate, restore'])


class CutoverSafety(Fixture):
    def test_symlinked_scratch_parent(self):
        fake_home = self.base/'fake-home'
        fake_home.mkdir()
        scratch = self.base/'scratch'
        scratch.mkdir()
        alias = fake_home/'tmp'
        alias.symlink_to(scratch, target_is_directory=True)
        f = Fixture('runTest')
        try:
            with mock.patch.dict(os.environ, {'HOME': str(fake_home), 'TMPDIR': str(alias)}), \
                    mock.patch.object(tempfile, 'tempdir', None):
                f.setUp()
                self.assertEqual(f.apply(), 0)
                self.assertEqual(f.apply(revert=True), 0)
                self.assertEqual(f.cron, f.original_cron)
        finally:
            f.doCleanups()
        self.signed('AC7-scratch', ['fake HOME with symlinked TMPDIR: apply and revert'])

    def test_inventory_and_parent_boundaries(self):
        outside=self.base/'outside'
        outside.mkdir()
        sentinel=outside/'sentinel'
        sentinel.write_text('unchanged')
        self.assertEqual(self.apply(),0)
        data=json.loads((self.c.state/'record.json').read_text())
        data['images']['root/../outside/sentinel']=dict(kind='file',data='YmFk',mode=0o600)
        cut.atomic(self.c.state/'record.json',json.dumps(data).encode())
        self.assertEqual(self.apply(revert=True),1)
        self.assertEqual(sentinel.read_text(),'unchanged')
        # No restore destination can follow an unexpected parent symlink.
        self.assertRaises(cut.Failure,self.c.path,'root/../../outside/sentinel')
        self.assertEqual(self.events.count('tg-check'),1)
        self.signed('AC7',['out-of-inventory restore metadata rejected','outside sentinel unchanged','structured fact arguments in AC3'])

    def test_parent_link_and_unknown_shapes(self):
        import shutil
        outside=self.base/'outside'; outside.mkdir()
        (outside/'sentinel').write_text('safe')
        shutil.rmtree(self.root/'telegram')
        (self.root/'telegram').symlink_to(outside,target_is_directory=True)
        self.assertEqual(self.apply(),1)
        self.assertEqual((outside/'sentinel').read_text(),'safe')
        self.assertFalse((outside/'tg-send.sh').exists())
        self.assertEqual(self.events,[])
        self.signed('AC7-parent',['symlink parent rejected before backup/installation'])


class CutoverPoller(Fixture):
    def test_generation_matrix(self):
        cases=[]
        for mode in ('stale','stop-fail','no-start','wrong-source','ambiguous','source-change','link-change'):
            f=Fixture('runTest'); f.setUp()
            try:
                f.mode=mode
                sleep=f.sleep
                def changed(seconds):
                    sleep(seconds)
                    if mode=='source-change':
                        (f.repo/'scripts/tg-poll.py').write_text('changed')
                    if mode=='link-change':
                        (f.root/'telegram/tg-poll.py').unlink()
                        (f.root/'telegram/tg-poll.py').symlink_to(f.repo/'scripts/pulse.py')
                with mock.patch.object(f,'sleep',side_effect=changed):
                    self.assertEqual(f.apply(),1,mode)
                f.assert_stopgaps()
                self.assertNotIn('tg-check',f.events)
                self.assertTrue((f.c.state/'record.json').is_file())
                cases.append(dict(case=mode,old_pid=10,old_generation=1,events=f.events))
            finally:
                f.doCleanups()
        self.signed('AC8-negative',cases)

    def test_old_code_generation(self):
        old=dict(self.process)
        keepalive=self.root/'telegram/tg-keepalive.sh'
        keepalive.write_text('# existing scheduler-owned keepalive\n')
        before=keepalive.read_bytes()
        installed_at=self.clock
        self.assertEqual(self.apply(),0)
        self.assertNotEqual(self.process['generation'],old['generation'])
        self.assertGreater(self.process['started'],installed_at)
        self.assertEqual([s for s in self.events if s.startswith('stop:')],['stop:10'])
        self.assertEqual(keepalive.read_bytes(),before)
        self.signed('AC8-old-code',dict(old=old,new=self.process,link_install_time=installed_at,events=self.events))

    def test_absent_repeat_and_restored_source(self):
        self.process=None
        self.assertEqual(self.apply(),0)
        installed=dict(self.process)
        self.assertEqual(installed['source'],str(self.repo/'scripts/tg-poll.py'))
        starts=self.events.count('keepalive-start')
        self.assertEqual(self.apply(),0)
        self.assertEqual(self.events.count('keepalive-start'),starts)
        self.assertEqual(self.apply(revert=True),0)
        self.assertNotEqual(self.process['generation'],installed['generation'])
        self.assertEqual(self.process['source'],str(self.root/'telegram/tg-poll.py'))
        self.signed('AC8',dict(installed=installed,restored=self.process,events=self.events))


if __name__ == '__main__':
    unittest.main()
