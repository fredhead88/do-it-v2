#!/usr/bin/env python3
"""SD10 scratch acceptance groups; selectors are unittest class names."""
import contextlib
import inspect
import hashlib
import io
import resource
from datetime import timedelta
import json
import os
import pathlib
import subprocess
import tempfile
from unittest.mock import patch

# Imports and CLI children must never acquire the operator's ledger.
_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix='doit-migration-import-')
os.environ['DOIT_ROOT'] = _IMPORT_ROOT.name

# Regression: rechain-first import must tolerate audit on its import cycle.
import rechain
import audit
import autodispatch
import dependency_holds
import dispatch
import fold
import sys
import unittest

import migration_cut
from rechain import SHARED, TEST, VERSIONS

VERSION = VERSIONS + 'new_orders.py'


def mixed(**changes):
    return dict(name='orders', id='L-spec-1000', footprint=[VERSION, 'app.py'],
                produces=['orders.invoice_id column'], code_exceeds_size=False, **changes)


class SharedColumnRegression:
    def test_shared_column(self):
        advice = migration_cut.needs_cut(mixed(), [dict(name='consumer', consumes=['orders.invoice_id column'])])
        self.assertEqual((advice.cut, advice.consumers, advice.migration_paths),
                         (True, ['consumer'], [VERSION]))
        print('shared-column:', advice)


class Scratch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.content = self.root / 'content'
        self.repo.mkdir()
        self.content.mkdir()
        (self.root / 'events').mkdir()
        self.write('app.py', 'pass\n')
        self.write('consumer.py', 'pass\n')
        self.enterContext(patch.object(fold, 'ROOT', self.root))
        self.enterContext(patch.object(dispatch, 'CONTENT', self.content))

    def write(self, path, body):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        return target

    def consumer(self, sid='L-spec-1001', text=None):
        (self.content / (sid + '.md')).write_text(text or
            '## Interfaces\nConsumes:\n- orders.invoice_id column\nProduces:\n- function() -> None\n')
        return event('spec-written', sid)

    def block(self, consumer=True, footprint=None, produces='orders.invoice_id column'):
        text = ('## orders\nGoal: create schema\nWave: 1\nDelivers: R1\nFootprint: ' +
                (footprint or VERSION + ' app.py') + '\nProduces: ' + produces + '\n')
        if consumer:
            text += ('## application\nWave: 2\nFootprint:\n- consumer.py\n'
                     'Consumes:\n- orders.invoice_id column\n')
        return text + '\n## Acquisition decisions\n- None\n'

    def audit(self, text=None, events=None):
        return audit.prepass(self.block() if text is None else text,
                             repo=self.repo, events=[] if events is None else events)


def event(kind, sid='L-spec-1001', ts='2026-01-01T00:00:00Z', **extra):
    return dict(type=kind, subject=sid, ts=ts, actor='operator', _src='operator:1', **extra)


class ConsumerAdviceTests(SharedColumnRegression, Scratch):
    def test_exact_schema_matrix(self):
        for artifact in ('orders.invoice_id column', 'orders table', 'invoice_state enum'):
            producer = mixed()
            producer['produces'] = [artifact]
            advice = migration_cut.needs_cut(producer, [dict(name='slot', consumes=[artifact, artifact])])
            self.assertEqual(advice, migration_cut.CutAdvice(True, ['slot'], [VERSION]))
            print('declared schema:', artifact, advice)
        negatives = ['invoices.invoice_id column', 'orders.invoice_id_extra column',
                     'orders.invoice_id table', 'orders table', 'orders.invoice_id() -> column']
        for seam in negatives:
            advice = migration_cut.needs_cut(mixed(), [dict(name='unrelated', consumes=[seam])])
            self.assertEqual(advice, migration_cut.CutAdvice(False, [], [VERSION]))
            print('negative exact-match:', seam, advice)
        producer = mixed()
        producer['produces'] = ['orders table']
        self.assertFalse(migration_cut.needs_cut(producer, [dict(name='column', consumes=['orders.invoice_id column'])]).cut)
        producer['produces'] = ['orders table', 'orders.invoice_id column']
        self.assertEqual(migration_cut.needs_cut(producer, [
            dict(name='z', consumes=['orders table']), dict(name='a', consumes=['orders.invoice_id column']),
            dict(name='z', consumes=['orders table']), producer]).consumers, ['a', 'z'])
        producer['produces'] = ['orders() -> Result']
        self.assertFalse(migration_cut.needs_cut(producer, [dict(name='a', consumes=['orders() -> Result'])]).cut)
        self.assertEqual(migration_cut.schema(' **`orders.invoice_id`** column '), ('orders.invoice_id', 'column'))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            migration_cut.needs_cut(dict(producer, produces=['orders table extra']), [])

    def test_holds_and_grants(self):
        for other, expected in [
            (dict(name='direct', text='Dependency-hold: waits_for=L-spec-1000'), ['direct']),
            (dict(name='ledger', waits_for=['L-spec-1000']), ['ledger']),
            (dict(name='unrelated', waits_for=['L-spec-9999']), []),
            (dict(name='example', text='```\nDependency-hold: waits_for=L-spec-1000\n```'), []),
            (dict(id='L-spec-1000', consumes=['orders.invoice_id column']), [])]:
            advice = migration_cut.needs_cut(mixed(), [other])
            self.assertEqual(advice.consumers, expected)
            print('hold/self:', other, advice)
        with self.assertRaisesRegex(ValueError, 'Dependency-hold'):
            migration_cut.needs_cut(mixed(), [dict(name='bad', text='Dependency-hold: waits_for=slot')])
        producer = mixed()
        producer['footprint'] = [VERSION, VERSION, *SHARED, TEST]
        self.assertEqual(migration_cut.needs_cut(producer, [dict(name='live', consumes=['orders.invoice_id column'])]),
                         migration_cut.CutAdvice(False, [], [VERSION]))
        producer['footprint'] = ['app.py']
        self.assertEqual(migration_cut.needs_cut(producer, []), migration_cut.CutAdvice(False, [], []))
        producer['footprint'] = [VERSIONS + '__init__.py', 'app.py']
        self.assertFalse(migration_cut.needs_cut(producer, []).cut)
        producer['footprint'] = [VERSIONS + 'z.py', VERSION, 'app.py']
        self.assertEqual(migration_cut.needs_cut(producer, []).migration_paths, [VERSION, VERSIONS + 'z.py'])

    def test_state_predicate(self):
        written = self.consumer()
        real_cases = {'written': [written], 'killed': [written, event('spec-killed')],
                      'void': [],
                      'closed-unbuilt': [written, event('spec-closed')],
                      'closed-shipped': [written, event('shipped'), event('spec-closed')],
                      'shipped': [written, event('shipped')]}
        base = [written, event('shipped', ts=(fold.NOW - timedelta(days=5)).isoformat())]
        real_cases['accepted'] = base + [event('verdict', confirmed=True), event('review', depth='gates-only')]
        for days, state in ((7, 'shipped-owed-due'), (2, 'shipped-owed-evidence')):
            real_cases[state] = base + [dict(event('owed-ac',
                ts=(fold.NOW - timedelta(days=10)).isoformat(), criterion='AC1',
                wake_at=(fold.NOW - timedelta(days=days)).isoformat()), actor='spec-writer')]
        real_cases['void'] = [dict(event(t), actor='spec-writer') for t in ('spawn-started', 'spawn-failed')]
        for expected, events in real_cases.items():
            self.assertEqual(fold.spec_state(events, set()), expected)
            live = expected not in autodispatch.TERMINAL_STATES
            self.assertEqual(bool(audit._open_migration_specs(events)), live)
            self.assertEqual('migration-not-cut' in self.audit(self.block(False), events), live)
            print('real fold state/predicate:', expected, live)
        for state in (*autodispatch.TERMINAL_STATES, 'shipped-owed-due',
                      'shipped-owed-evidence', 'shipped-owed-expired', 'shipped'):
            with patch.object(fold, 'spec_state', return_value=state):
                specs = audit._open_migration_specs([written])
                self.assertEqual(migration_cut.needs_cut(mixed(), specs).cut,
                                 state not in autodispatch.TERMINAL_STATES)
                self.assertEqual('migration-not-cut' in self.audit(self.block(False), [written]),
                                 state not in autodispatch.TERMINAL_STATES)
            print('explicit folded-state vocabulary injection:', state,
                  state not in autodispatch.TERMINAL_STATES)

    def test_snapshot_holds(self):
        sid = 'L-spec-1001'
        text = 'Dependency-hold: waits_for=L-spec-1000'
        written = event('spec-written')
        hold = event('dependency-hold', waits_for='L-spec-1000')
        release = event('unblocked', ts='2026-01-02T00:00:00Z')
        with patch.object(dependency_holds, 'text_hold_reason', side_effect=AssertionError('live text')), \
             patch.object(dispatch, 'spec_path', side_effect=AssertionError('live file')):
            for label, own, snapshot, expected in [
                ('text marker', [written], [written], ['L-spec-1000']),
                ('no invented provenance', [], [], []),
                ('newer authorized release', [written, release], [written, release], []),
                ('equal-time non-release', [written, dict(release, ts=written['ts'])], [], ['L-spec-1000']),
                ('unauthorized non-release', [written, dict(release, actor='builder')], [], ['L-spec-1000']),
                ('authoritative released ledger over text', [written, hold, release], [written, hold, release], []),
                ('satisfied prerequisite', [written], [written, event('shipped', 'L-spec-1000')], []),
                ('newest unrelated ledger', [written, hold, dict(hold, waits_for='L-spec-2222',
                     ts='2026-01-03T00:00:00Z')], [], ['L-spec-2222'])]:
                result = audit._snapshot_holds(sid, text, own, snapshot)
                self.assertEqual(result, expected)
                print('snapshot hold:', label, result)
            same_time = dict(hold, waits_for='L-spec-2222', _src='operator:2')
            self.assertEqual(audit._snapshot_holds(sid, text, [written, hold, same_time], []), ['L-spec-2222'])
            ref = event('decision', ts=release['ts'], ref='spec-text:' + sid)
            self.assertEqual(audit._snapshot_holds(sid, text, [written, ref], []), [])
            with self.assertRaisesRegex(ValueError, 'invalid'):
                audit._snapshot_holds(sid, '', [dict(hold, waits_for='bad')], [])


class SizeAdviceTests(Scratch):
    def test_boundaries(self):
        with patch.object(audit, 'CEILING', audit.FLOOR + 10):
            for label, count, expected in [('below', 36, False), ('equal', 40, False), ('above', 44, True)]:
                self.write('app.py', 'x' * count)
                self.write(VERSION, 'x' * 10000)
                for path in SHARED:
                    self.write(path, 'x' * 10000)
                slot = mixed()
                slot['footprint'] += sorted(SHARED)
                decision, estimate = audit.code_measure(slot, self.repo)
                self.assertEqual(decision, expected)
                slot['code_exceeds_size'] = decision
                self.assertEqual(migration_cut.needs_cut(slot, []).cut, expected)
                print('code-only', label, 'estimate', estimate, 'ceiling', audit.CEILING)
            self.write('app.py', '')
            self.assertEqual(audit.code_measure(slot, self.repo), (False, audit.FLOOR))
            self.assertNotIn('migration-not-cut', self.audit(self.block(False)))
            (self.repo / 'app.py').unlink()
            self.assertEqual(audit.code_measure(slot, self.repo)[0], None)
            slot['code_exceeds_size'] = None
            with self.assertRaisesRegex(ValueError, 'unmeasured'):
                migration_cut.needs_cut(slot, [])
            print('missing code:', self.audit(self.block(False)).split('migration-cut (SD10):')[1])
            self.assertIn('migration-cut (SD10): undetermined', self.audit(self.block(False)))
            self.assertTrue(migration_cut.needs_cut(slot, [dict(name='known', consumes=['orders.invoice_id column'])]).cut)
            slot['footprint'] = ['app.py']
            slot['code_exceeds_size'] = True
            self.assertFalse(migration_cut.needs_cut(slot, []).cut)

    def test_missing_sources_and_globs(self):
        unreadable = event('spec-registered')
        result = self.audit(self.block(False), [unreadable])
        self.assertIn('live-spec declarations unreadable', result)
        glob = self.block(False, VERSIONS + '*.py app.py')
        result = self.audit(glob)
        self.assertIn('unresolved migration grant', result)
        explicit = mixed()
        explicit['footprint'] += [VERSIONS + '*.py']
        self.assertTrue(migration_cut.needs_cut(explicit,
            [dict(name='consumer', consumes=['orders.invoice_id column'])]).cut)
        self.assertIn('migration-not-cut', self.audit(self.block(True, VERSIONS + '*.py ' + VERSION + ' app.py')))
        self.write(VERSION, 'pass\n')
        self.assertIn('migration-not-cut', self.audit(self.block(True, VERSIONS + '*.py app.py')))
        print('unreadable registered content / unresolved / resolved globs:', result)


class AuditIntegrationTests(Scratch):
    def test_live_and_terminal_consumers(self):
        ConsumerAdviceTests.test_state_predicate(self)

    def test_production_routes_and_cli(self):
        block = self.block()
        for output in (self.audit(block), audit.prepass_two_file('plan', block, plan_text=block,
                                                                 repo=self.repo, events=[])):
            self.assertIn('migration-not-cut: orders — application', output)
            print('in-process prepass:', output)
        self.assertIn('migration-cut (SD10)', audit.prepass_two_file('cut', block, events=[]))
        written = self.consumer()
        self.assertIn('L-spec-1001', self.audit(self.block(False), [written]))
        # Registered and awaiting specs need not have a spec-written marker.
        for kind in ('spec-registered', 'spec-awaiting'):
            self.assertIn('L-spec-1001', self.audit(self.block(False), [event(kind)]))
        (self.root / 'events' / 'spec-writer-0001.jsonl').write_text(json.dumps(
            dict(written, type='spec-written', charter='L-charter-9999')) + '\n')
        plan = self.root / 'plan.md'
        plan.write_text(self.block(False))
        charter = self.root / 'charter.md'
        charter.write_text('## Requirements\n- R1: create schema\n')
        env = dict(os.environ, DOIT_ROOT=str(self.root), DOIT_READ_CACHE='0', DOIT_PROJECT='')
        for stage in ('plan', 'doc'):
            args = ['--doc', str(plan)] if stage == 'doc' else ['--cut', str(plan), '--plan', str(plan)]
            out = self.root / 'out.md'
            proc = subprocess.run(['./doit', 'audit', stage, 'L-charter-0001', *args,
                '--charter', str(charter), '--repo', str(self.repo), '--out', str(out)],
                cwd=pathlib.Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, out.read_text())
            self.assertIn('migration-not-cut: orders — L-spec-1001', proc.stdout)
            print('CLI', stage, proc.returncode, proc.stdout)
        corrected = self.block(False, VERSION + ' ' + ' '.join(sorted(SHARED)))
        corrected += '\n## application\nWave: 2\nFootprint: app.py\nConsumes: orders.invoice_id column\n'
        self.assertNotIn('migration-not-cut', self.audit(corrected, [written]))
        with patch.object(audit, 'CEILING', audit.FLOOR):
            output = self.audit(self.block(False))
            self.assertIn('code-size estimate=', output)
            print('size-only missed cut:', output)

    def test_snapshot_and_read_failures(self):
        with patch.object(fold, 'read_events', return_value=[]) as reader:
            self.assertIn('migration-not-cut', audit.prepass(self.block(), repo=self.repo))
            self.assertEqual(reader.call_count, 1)
        with patch.object(fold, 'read_events', side_effect=AssertionError('hidden ledger')), \
             patch.object(dependency_holds, 'text_hold_reason', side_effect=AssertionError('hidden file')):
            self.assertIn('application', self.audit())
        sid = 'L-spec-1001'
        written = self.consumer(text='## Interfaces\nConsumes:\n\nDependency-hold: waits_for=L-spec-1000\n')
        producer = self.block(False).replace('## orders', '## L-spec-1000 orders')
        release = event('unblocked', ts='2026-01-02T00:00:00Z')
        with patch.object(fold, 'read_events', return_value=[written, release]):
            self.assertIn('migration-not-cut', self.audit(producer, [written]))
        with patch.object(fold, 'read_events', side_effect=AssertionError('ignore real ledger')):
            self.assertNotIn('migration-not-cut', self.audit(producer, [written, release]))
        with patch.object(fold, 'read_events', side_effect=OSError('secret omitted')):
            result = audit.prepass(self.block(False), repo=self.repo)
            self.assertIn('ledger/content unreadable', result)
            self.assertNotIn('secret omitted', result)
        plan = producer + '\n## L-spec-1001 application\nWave: 2\nFootprint: consumer.py\nDependency-hold: waits_for=L-spec-1000\n'
        self.assertIn('migration-not-cut', self.audit(plan, [written]))
        self.assertNotIn('migration-not-cut', self.audit(plan, [written, release]))
        self.assertNotIn('migration-not-cut', self.audit(plan, []))
        (self.content / (sid + '.md')).unlink()
        self.assertIn('undetermined', self.audit(producer, [written]))

class ContractRuleTests(Scratch):
    def test_contract_paragraphs_and_seeded_cut(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        for role in ('planner', 'spec-writer'):
            text = (repo / 'agents' / (role + '.md')).read_text()
            start = text.index('**SD10 — migration unit cut.**')
            end = text.index('\n\n', text.index('This rule grants no application-code writes.', start))
            rule = text[start:end]
            for clause in ('alembic version AND', 'column, table or\nenum', 'Consumes:',
                           'Interfaces', 'dependency hold', 'OR (b)', '§4.3',
                           'migration-only predecessor merged first', 'rechain.VERSIONS',
                           'rechain.SHARED', 'rechain.TEST', 'application code belongs'):
                self.assertIn(clause, rule)
            print('actual contract rule:', role, rule)
            self.assertIn('orders-migration', text)
            self.assertIn('orders-code', text)
            self.assertIn('rewir', text)
            if role == 'spec-writer':
                for marker in ('split-proposed', 'Never shrink the original Goal', 'allocate ids',
                               'write two specs yourself', 'content-only'):
                    self.assertIn(marker, text)
            else:
                self.assertIn('real-id dependency hold', text)
                self.assertIn('allocation/re-carve', text)
        artifact = ('## L-spec-2000 orders-migration\nWave: 1\nFootprint: ' + VERSION +
                    ' ' + ' '.join(sorted(SHARED)) + '\nProduces: orders.invoice_id column\n'
                    '## L-spec-2001 orders-code\nWave: 2\nFootprint: app.py\n'
                    'Consumes: orders.invoice_id column\nDependency-hold: waits_for=L-spec-2000\n'
                    '## other-schema-consumer\nWave: 2\nFootprint: consumer.py\n'
                    'Consumes: orders.invoice_id column\n## Acquisition decisions\n- None\n')
        units = audit.units(artifact)
        self.assertEqual(len(units), 3)
        self.assertTrue(all(migration_cut.is_version(p) or p in SHARED or p == TEST
                            for p in units[0]['footprint']))
        self.assertFalse(any(migration_cut.is_version(p) for p in units[1]['footprint']))
        self.assertEqual(dependency_holds.from_spec_text(units[1]['text']), 'L-spec-2000')
        self.assertFalse(migration_cut.needs_cut(units[0], units).cut)
        self.assertNotIn('migration-not-cut', self.audit(artifact, [self.consumer()]))
        print('validated seeded migration-first/code-second declarations:', artifact)


class CompatibilityTests(Scratch):
    def test_existing_regressions(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        scratch = self.root / 'scratch'
        scratch.mkdir()
        alias = self.root / 'runner-tmp'
        alias.symlink_to(scratch, target_is_directory=True)
        env = dict(os.environ, TMPDIR=str(alias), TMP=str(alias), TEMP=str(alias))
        # Keep both fixture paths and registry results on the same real path.
        # The runner may supply a symlink; retain its directory and all assertions.
        env.update({key: os.path.realpath(env[key]) for key in ('TMPDIR', 'TMP', 'TEMP')})
        for name in ('audit', 'contracts', 'checks', 'retired'):
            command = ['/usr/bin/python3', 'src/test_' + name + '.py']
            result = subprocess.run(command, cwd=repo, env=env, text=True, capture_output=True)
            print(' '.join(command), 'exit', result.returncode,
                  'assertion summary:', (result.stdout + result.stderr).strip()[-900:])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_interfaces_security_and_read_only(self):
        self.assertEqual(str(inspect.signature(audit.prepass)),
                         '(doc_text, charter_text=None, repo=None, events=None)')
        self.assertEqual(str(inspect.signature(audit.prepass_two_file)),
                         '(stage, cut_text, charter_text=None, plan_text=None, repo=None, events=None)')
        repo = pathlib.Path(__file__).resolve().parents[1]
        for name in ('planner', 'spec-writer'):
            relative = 'agents/' + name + '.md'
            text = (repo / relative).read_text()
            # The pinned base is the branch's parent once committed, and HEAD
            # during the pre-commit check. No mutable remote baseline is used.
            added = subprocess.run(['git', 'diff', '--name-only', 'HEAD', '--', relative],
                                   cwd=repo, text=True, capture_output=True).stdout
            base = 'HEAD' if added.strip() else 'HEAD^'
            old = subprocess.run(['git', 'show', base + ':' + relative], cwd=repo,
                                 text=True, capture_output=True, check=True).stdout
            self.assertEqual(text[text.index('## Lessons'):], old[old.index('## Lessons'):])
            self.assertTrue([line for line in text.splitlines() if line.startswith('## ')][-1].startswith('## Lessons'))
            if name == 'spec-writer':
                self.assertLessEqual(len(text.splitlines()), 1500)
        written = self.consumer()
        (self.root / 'events' / 'operator.jsonl').write_text(json.dumps(written) + '\n')
        before = {p.relative_to(self.root).as_posix(): p.read_bytes()
                  for p in self.root.rglob('*') if p.is_file()}
        with contextlib.ExitStack() as stack:
            for owner, name in ((subprocess, 'run'), (subprocess, 'Popen'), (os, 'system'),
                                (dispatch, 'emit'), (fold, 'read_events')):
                stack.enter_context(patch.object(owner, name, side_effect=AssertionError('mutation/hidden reader')))
            import urllib.request
            stack.enter_context(patch.object(urllib.request, 'urlopen', side_effect=AssertionError('network')))
            result = self.audit(events=[written])
            self.assertIn(audit.HEADER, result)
            for row in ('same-wave footprint overlap', 'undefined seams', 'shared names introduced twice',
                        'requirement coverage', 'unit size', 'acquisition ADR trail', 'seam direction'):
                self.assertIn(row, result)
            self.assertIn('advisory only, never blocks a dispatch', result)
            hostile = '$(touch NEVER) <tag> "quote" ../looking'
            advice = migration_cut.needs_cut(mixed(), [dict(name=hostile,
                   text='```\nDependency-hold: waits_for=$(touch NEVER)\n```',
                   consumes=['orders.invoice_id column'])])
            self.assertEqual(advice.consumers, [hostile])
            odd = mixed()
            odd['footprint'] = [VERSIONS + '$(inert)<tag>"quote".py', 'app.py']
            self.assertEqual(migration_cut.needs_cut(odd, []).migration_paths,
                             [VERSIONS + '$(inert)<tag>"quote".py'])
            for path in ('../outside.py', '/absolute.py', VERSIONS + '../escape.py', '.env'):
                output = self.audit(self.block(False, path))
                self.assertIn('undetermined', output)
                self.assertNotIn('migration-not-cut', output)
            (self.repo / 'link.py').symlink_to(self.root / 'outside.py')
            self.assertIn('undetermined', self.audit(self.block(False, VERSION + ' link.py')))
            (self.repo / 'link.py').unlink()
        after = {p.relative_to(self.root).as_posix(): p.read_bytes()
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        print('signatures, seven rows, HEADER, advisory wording, inert hostile declarations; unchanged bytes:', sorted(after))


if __name__ == '__main__':
    body = io.StringIO()
    with contextlib.redirect_stdout(body):
        program = unittest.main(verbosity=2, exit=False)
    text = body.getvalue()
    print(text, end='')
    result = program.result
    summary = f'{result.testsRun} tests; {len(result.failures)} failures; {len(result.errors)} errors'
    print(json.dumps(dict(url='scratch://migration-cut/' + (sys.argv[1] if len(sys.argv) > 1 else 'all'),
                          status=0 if result.wasSuccessful() else 1,
                          body_sha256=hashlib.sha256(text.encode()).hexdigest(), body_excerpt=summary)))
    own, children = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    print('COMPUTE_METER cpu_seconds=%.3f' % (own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime))
    _IMPORT_ROOT.cleanup()
    sys.exit(0 if result.wasSuccessful() else 1)
