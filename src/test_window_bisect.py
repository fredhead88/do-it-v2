"""Scratch lane disposition contracts; CI transport is never live."""
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
from unittest.mock import patch


_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix='window-bisect-import-')
os.environ['DOIT_ROOT'] = _IMPORT_ROOT.name
os.environ['DOIT_NO_POKE'] = '1'
SRC = Path(__file__).resolve().parent
NODE = 'api/tests/test_total.py::test_total[param::complete]'
OTHER = 'api/tests/test_total.py::test_other'


def receipt(name, body):
    text = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='scratch://window-bisect/'+name, status=200,
                          body_sha256=hashlib.sha256(text.encode()).hexdigest(), body_excerpt=text)))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='window-bisect-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.queue = self.root / 'lane/rq'
        self.queue.mkdir(parents=True)
        self.ready = self.root / 'lane/ready-pass.txt'
        self.repo = self.root / 'repos/demo'
        self.repo.mkdir(parents=True)
        self.env = dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1',
                        DOIT_BISECT_QUEUE_DIR=str(self.queue),
                        DOIT_BISECT_READY_FILE=str(self.ready), DOIT_READ_CACHE='0',
                        DOIT_WINDOW_RECHAIN='off', DOIT_GATE_LEDGER_FILE='L-executor-bisect.jsonl')
        for key in list(self.env):
            if key.startswith(('GIT_', 'SUPABASE_DB_URL')):
                self.env.pop(key)
        self.git('init', '-q', '-b', 'master')
        (self.repo / 'base.txt').write_text('base\n')
        checks = self.repo / 'scripts/ci/predeploy_gate.sh'
        checks.parent.mkdir(parents=True)
        checks.write_text('REQUIRED_CHECKS=("pytest" "pg-parity (Postgres)" "TypeScript · Biome · Tests · File Sizes")\n')
        self.commit()
        self.base = self.git('rev-parse', 'HEAD')
        self.members = ['L-spec-9001', 'L-spec-9002', 'L-spec-9003', 'L-spec-9004']
        for spec in self.members:
            self.git('switch', '-qc', spec, self.base)
            (self.repo / (spec + '.txt')).write_text(spec)
            self.commit()
            content = self.root / 'content'
            content.mkdir(exist_ok=True)
            (content / (spec + '.md')).write_text('Writes: ' + spec + '.txt\n')
        self.git('switch', '-q', 'master')
        self.ident = 'rh-fixture'
        self.batch = self.queue / '123.batch.red'
        self.idsfile = self.root / 'ids.json'
        self.idsfile.write_text('[]')
        (self.queue / 'bisect-protocol.json').write_text(json.dumps(dict(
            version=1, retry_reader=True, shared_writers_lock=True)))
        self.events = self.root / 'events'
        self.events.mkdir()
        self.record('undetermined')

    def git(self, *args):
        proc = subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=fixture',
                               '-c', 'user.email=fixture@localhost', *args],
                              env=self.env, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture')

    def record(self, state, ids=(), **fields):
        from datetime import datetime, timezone
        self.row = dict(v=1, ts=datetime.now(timezone.utc).isoformat(), type='window-rehearsed',
                        subject=self.ident, project='demo', base_sha='unresolved',
                        specs=self.members, state=state, **fields)
        if ids:
            self.row['ids'] = list(ids)
        rows = [dict(v=1, ts=self.row['ts'], type='merge-gate-clean', subject=s,
                     branch=s, project='demo') for s in self.members] + [self.row]
        (self.events / 'L-executor-fixture.jsonl').write_text(
            ''.join(json.dumps(row) + '\n' for row in rows))
        self.batch.write_text(self.base + ' ' + ' '.join(s[7:] for s in self.members) + '\n')
        Path(str(self.batch) + '.ci').write_text('exit=2\n')
        self.idsfile.write_text(json.dumps(list(ids)))

    def cli(self, extra=(), script=None):
        args = [self.ident, '--project', 'demo', '--specs', ','.join(self.members),
                '--batch', str(self.batch), '--failed-ids', str(self.idsfile)] + list(extra)
        argv = [sys.executable, str(SRC.parent / 'doit')]
        if script is None:
            argv = ['bash', str(SRC.parent / 'doit'), 'window-bisect', *args]
        else:
            argv = [sys.executable, '-c', script, str(SRC), *args]
        proc = subprocess.run(argv, env=self.env, text=True, capture_output=True, timeout=30)
        return proc

    def requests(self):
        return [json.loads(p.read_text()) for p in sorted((self.queue / 'bisect-retry').glob('*.json'))]

    def red(self, ids=(NODE,), stages=False):
        self.record('red', ids)
        script = '''import sys,json;sys.path.insert(0,sys.argv[1])
import window_bisect as w
events=w.admitted_events();row=events[-1];repo=w.fold.ROOT/'repos/demo'
entries=w.gate.resolve_entries(repo,row['specs'],events)
candidate=w.window_rehearse.prepare(repo,row['subject'],sys.argv[2],entries,'demo','master',events)
print(json.dumps(dict(sha=candidate,tree=w.gate.tree_of(repo,candidate),required=w.gate.required_checks(repo,sys.argv[2]))))'''
        proc = subprocess.run([sys.executable, '-c', script, str(SRC), self.base],
                              env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.row.update(json.loads(proc.stdout), base_sha=self.base)
        rows = [dict(v=1, ts=self.row['ts'], type='merge-gate-clean', subject=s,
                     branch=s, project='demo') for s in self.members]
        if stages:
            rows.extend(dict(self.row, state=state) for state in ('prepared', 'running'))
        rows.append(self.row)
        (self.events / 'L-executor-fixture.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def transport(self, *, known=False, failing=(), before=''):
        # Only external CI is stubbed. Git, admission, CLI, files and fold remain real.
        return '''import sys,json;sys.path.insert(0,sys.argv[1])
import window_bisect as w
def forbidden(*a,**k): raise AssertionError('unexpected live transport')
w.attribution._gh=forbidden
def classify(project,base,ids):
    assert project=='demo'
    return dict(known_red=ids if KNOWN else [],rerun_on_base=[] if KNOWN else ids)
def rerun(project,base,ids):
    return dict(pre_existing=[],new=ids,undetermined=False)
def probe(repo,sha,ids):
    present=w.gate.git(repo,'ls-tree','--name-only',sha).stdout.splitlines()
    group=[s for s in FAILING if s+'.txt' in present]
    print(json.dumps(dict(probe=sha,ids=ids,present=present)),file=sys.stderr)
    return dict(sha=sha,states=dict.fromkeys(ids,'red' if group else 'green'))
w.base_red.classify_selected=classify
w.base_red.rerun_on_base=rerun
w.attribution.probe=probe
'''.replace('KNOWN', repr(known)).replace('FAILING', repr(list(failing))) + before + '\nsys.exit(w.main(sys.argv[2:]))'

    def consumer(self, request, ident, epoch, *, interrupt=False):
        # Contract reader in another process: exact groups, shared lock, durable claim/ack.
        script = '''import sys,json;sys.path.insert(0,sys.argv[1])
import window_bisect as w
q=w.Path(sys.argv[2]);p=q/'bisect-retry'/sys.argv[3]
with w.lane_lock(q):
 r=w.read_json(p)
 if r['state']=='acked': sys.exit(3)
 if r['state']=='pending':
  r.update(state='claimed',claimed_batch=sys.argv[5]+'.batch',claimed_rehearsal=sys.argv[4]);w.save(p,r)
 name=r['claimed_batch'];ident=r['claimed_rehearsal']
 guard=q/(name+'.ci')
 if not guard.exists(): w.atomic(guard,b'pending\\n')
 if sys.argv[7]=='interrupt': sys.exit(4)
 meta=dict(version=1,request_id=r['request_id'],project=r['project'],lineage=r['lineage'],root_rehearsal=r['root_rehearsal'],rehearsal=ident,members=r['members'],attempt=r['attempt'])
 w.save(q/(name+'.lineage.json'),meta)
 w.atomic(q/name,(sys.argv[6]+' '+' '.join(s[7:] for s in r['members'])+'\\n').encode())
 r['state']='acked';w.save(p,r)
 print(json.dumps(r))'''
        args = [sys.executable, '-c', script, str(SRC), str(self.queue),
                request['request_id'] + '.json', ident, str(epoch), self.base,
                'interrupt' if interrupt else 'complete']
        return subprocess.run(args, env=self.env, text=True, capture_output=True, timeout=20)

    def launch_predicate(self, batch):
        # The two existing rloop.sh poll/launch guards, with side effects removed.
        script = '''h=$1
if [ -f "$h.ci" ] && ! grep -q '^exit=' "$h.ci"; then echo pending
elif [ -f "$h.ci" ] && ! grep -q '^exit=0' "$h.ci"; then echo not-green
else echo launch
fi'''
        return subprocess.run(['bash', '-c', script, 'poll', str(batch)], env=self.env,
                              capture_output=True, text=True, check=True).stdout.strip()

    def consume(self, request, ident, epoch):
        proc = self.consumer(request, ident, epoch)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        claimed = json.loads(proc.stdout)
        duplicate = self.consumer(request, 'rh-other-consumer', epoch + 100)
        self.assertEqual(duplicate.returncode, 3)
        name = self.queue / claimed['claimed_batch']
        guard = Path(str(name) + '.ci')
        self.assertEqual(self.launch_predicate(name), 'pending')
        self.assertEqual(name.read_text().split()[1:], [s[7:] for s in request['members']])
        guard.write_text('exit=2\n')
        self.assertEqual(self.launch_predicate(name), 'not-green')
        guard.write_text('exit=0\n')
        self.assertEqual(self.launch_predicate(name), 'launch')
        receipt('consumer', dict(request=request, claimed=claimed,
                                guards=['pending', 'not-green', 'launch']))
        self.ident, self.members = claimed['claimed_rehearsal'], request['members']
        self.batch = Path(str(name) + '.red')
        name.rename(self.batch)
        guard.rename(Path(str(self.batch) + '.ci'))
        return json.loads(proc.stdout)

    def ledger(self):
        script = 'import sys,json;sys.path.insert(0,sys.argv[1]);import fold;print(json.dumps(fold.read_events(use_cache=False)))'
        proc = subprocess.run([sys.executable, '-c', script, str(SRC)], env=self.env,
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def success(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        groups = [result[k] for k in ('culprits', 'innocent', 'requeued')]
        self.assertEqual(sorted(sum(groups, [])), sorted(self.members))
        self.assertEqual(len(set(sum(groups, []))), len(self.members))
        self.assertIn(result['depth'], range(3))
        body = json.dumps(result, sort_keys=True)
        print(json.dumps(dict(url='scratch://window-bisect/' + self.id(), status=200,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                              body_excerpt=body)))
        return result


class CliTests(Fixture):
    def test_normal_exception_and_capacity_undetermined_shapes_skip_branch_resolution(self):
        variants = [dict(base_sha=self.base,sha='a'*40,tree='b'*40,required=['pytest']),
                    dict(base_sha=self.base), dict(base_sha='unresolved')]
        for index,fields in enumerate(variants):
            self.ident='rh-uncertain-shape-'+str(index)
            self.record('undetermined')
            self.row.update(fields)
            (self.events/'L-executor-fixture.jsonl').write_text(json.dumps(self.row)+'\n')
            script=self.transport(before='w.gate.resolve_entries=forbidden;w.window_rehearse.prepare=forbidden;w.base_red.classify_selected=forbidden')
            self.success(self.cli(script=script))

    def test_red_without_named_ids_retries_without_probe(self):
        self.red(ids=())
        result=self.success(self.cli(script=self.transport(before='w.base_red.classify_selected=forbidden;w.attribution.probe=forbidden')))
        self.assertEqual(result['requeued'],self.members)
        self.assertEqual(self.requests()[0]['kind'],'whole')

    def test_red_identity_with_prepared_and_running_records(self):
        self.red(stages=True)
        before = self.git('show-ref')
        result = self.success(self.cli(script=self.transport(known=True)))
        self.assertEqual(result['innocent'], self.members)
        self.assertEqual(self.git('show-ref'), before)
        self.assertEqual(self.ready.read_text().splitlines(), [s[7:] for s in self.members])

    def test_moving_branch_refuses_before_base_probe_or_mutation(self):
        self.red()
        self.git('switch', '-q', self.members[0])
        (self.repo / 'moved.txt').write_text('changed')
        self.commit()
        before = self.batch.read_bytes()
        proc = self.cli(script=self.transport(before="w.base_red.classify_selected=forbidden"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.batch.read_bytes(), before)
        self.assertFalse(self.requests())
        self.assertFalse((self.queue / '.bisect.lock').exists())

    def test_helper_diagnostics_are_redacted_and_stdout_is_one_json_object(self):
        self.red()
        script = self.transport(known=True, before='''
original = w.window_rehearse.prepare
def noisy(*args, **kwargs):
 print('token=' + 'DIAGNOSTIC_SENTINEL')
 print('password=' + 'STDERR_SENTINEL', file=sys.stderr)
 return original(*args, **kwargs)
w.window_rehearse.prepare = noisy
''')
        proc = self.cli(script=script)
        self.success(proc)
        self.assertNotIn('DIAGNOSTIC_SENTINEL', proc.stdout + proc.stderr)
        self.assertNotIn('STDERR_SENTINEL', proc.stdout + proc.stderr)
        self.assertIn('[redacted]', proc.stderr)

    def test_foreign_unknown_and_batch_mismatch_are_read_only(self):
        for fields in (dict(project='foreign'), dict(subject='rh-foreign')):
            self.record('undetermined')
            row = dict(self.row, **fields)
            (self.events / 'L-executor-fixture.jsonl').write_text(json.dumps(row)+'\n')
            self.assertNotEqual(self.cli().returncode, 0)
            self.assertTrue(self.batch.exists())
        self.record('undetermined')
        self.batch.write_text(self.base+' 9999\n')
        self.assertNotEqual(self.cli().returncode, 0)
        self.assertFalse(self.requests())

    def test_exception_and_capacity_uncertainty_need_no_candidate(self):
        result = self.success(self.cli())
        self.assertEqual(result['requeued'], self.members)
        self.assertFalse(self.batch.exists())

    def test_input_rejection_is_read_only(self):
        for extra in (['--project', '../demo'], ['--specs', ','.join(self.members[::-1])],
                      ['--specs', ','.join(self.members + self.members[:1])]):
            with self.subTest(extra=extra):
                before = self.batch.read_bytes()
                proc = self.cli(extra)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(self.batch.read_bytes(), before)
                self.assertFalse((self.queue / 'bisect-state').exists())

    def test_green_and_flake_green_are_refused(self):
        for fields in ({}, dict(flake_only=True)):
            self.record('green', **fields)
            self.assertNotEqual(self.cli().returncode, 0)
            self.assertTrue(self.batch.exists())
            self.assertFalse(self.requests())

    def test_failed_ids_must_equal_authoritative_complete_strings(self):
        self.record('undetermined', [NODE, OTHER])
        for ids in ([NODE], [NODE, OTHER, 'extra'], [NODE, NODE, OTHER],
                    [NODE.split('[')[0], OTHER], ['foreign']):
            self.idsfile.write_text(json.dumps(ids))
            self.assertNotEqual(self.cli().returncode, 0)
            self.assertFalse(self.requests())


class BaseClassificationTests(Fixture):
    def test_real_selective_stale_and_missing_anchors_still_rerun_recorded_base(self):
        import window_bisect as w
        from datetime import datetime, timedelta, timezone
        now=datetime.now(timezone.utc)
        for anchor in (None, dict(sha=self.base,ts=(now-timedelta(days=3)).isoformat()),
                       dict(sha=self.base,ts=now.isoformat())):
            since=anchor['ts'] if anchor else now.isoformat()
            known={NODE:dict(sha=self.base,since=since,source='nightly')}
            with patch.dict(os.environ,self.env), patch.object(w.fold,'ROOT',self.root), \
                 patch.object(w.base_red,'_known_state',return_value=(known,anchor)), \
                 patch.object(w.base_red,'rerun_on_base',return_value=dict(pre_existing=[],new=[NODE],undetermined=False)) as rerun:
                result=w.new_failures('demo',self.base,[NODE])
            self.assertEqual(result,[] if anchor and anchor['ts']==now.isoformat() else [NODE])
            if result: rerun.assert_called_once_with('demo',self.base,[NODE])
            else: rerun.assert_not_called()
            receipt('base-anchor-matrix',dict(anchor='missing' if anchor is None else 'fresh' if not result else 'stale',new=result,dispatches=rerun.call_count))

    def test_timeout_cancelled_and_unsupported_base_evidence_stays_unknown(self):
        import window_bisect as w
        for error in (subprocess.TimeoutExpired(['fixture'],1), OSError('cancelled'),
                      ValueError('unsupported'), w.gate.TrainCancelled('cancelled')):
            with patch.object(w.base_red,'classify_selected',return_value=dict(known_red=[],rerun_on_base=[NODE])), \
                 patch.object(w.base_red,'rerun_on_base',side_effect=error):
                self.assertIsNone(w.new_failures('demo','a'*40,[NODE]))

    def test_only_selected_unknown_ids_are_rerun(self):
        import window_bisect as wb
        with patch.object(wb.base_red, 'classify_selected', return_value=dict(
                known_red=[NODE], rerun_on_base=[OTHER], undetermined=True)) as classify, \
             patch.object(wb.base_red, 'rerun_on_base', return_value=dict(
                pre_existing=[], new=[OTHER], undetermined=False)) as rerun:
            self.assertEqual(wb.new_failures('demo', 'a' * 40, [NODE, OTHER]), [OTHER])
            classify.assert_called_once_with('demo', 'a' * 40, [NODE, OTHER])
            rerun.assert_called_once_with('demo', 'a' * 40, [OTHER])

    def test_known_and_reproduced_base_failures_do_not_incriminate(self):
        import window_bisect as wb
        for selected in (dict(known_red=[NODE], rerun_on_base=[], undetermined=False),
                         dict(known_red=[], rerun_on_base=[NODE], undetermined=True)):
            with patch.object(wb.base_red, 'classify_selected', return_value=selected), \
                 patch.object(wb.base_red, 'rerun_on_base', return_value=dict(
                     pre_existing=[NODE], new=[], undetermined=False)) as rerun:
                self.assertEqual(wb.new_failures('demo', 'a' * 40, [NODE]), [])
                self.assertEqual(rerun.call_count, bool(selected['rerun_on_base']))

    def test_malformed_and_unresolved_evidence_remains_unknown(self):
        import window_bisect as wb
        for result in ({}, dict(pre_existing=[], new=[], undetermined=True),
                       dict(pre_existing=[], new=['extra'], undetermined=False)):
            with patch.object(wb.base_red, 'classify_selected', return_value=dict(
                    known_red=[], rerun_on_base=[NODE], undetermined=True)), \
                 patch.object(wb.base_red, 'rerun_on_base', return_value=result):
                self.assertIsNone(wb.new_failures('demo', 'a' * 40, [NODE]))


class BoundedDiagnosisTests(Fixture):
    def test_cancelled_child_is_requeued_and_green_sibling_is_released(self):
        import window_bisect as w
        visits = []
        def judge(group, depth):
            visits.append((group, depth))
            if depth and self.members[0] in group:
                raise w.gate.TrainCancelled('cancelled')
            return {NODE: 'red' if depth == 0 else 'green'}
        result, proof = w.split_diagnosis(self.members, [NODE], judge)
        self.assertEqual(result, dict(culprits=[], innocent=self.members[2:],
                                     requeued=self.members[:2], depth=1))
        self.assertFalse(proof)
        receipt('cancelled-child', dict(visits=visits, result=result))

    def test_exact_tree_reuse_and_reject_stale_foreign_wrong_checks_and_ids(self):
        import window_bisect as w
        from datetime import datetime, timedelta, timezone
        self.members = self.members[:1]
        self.red()
        current = datetime.now(timezone.utc)
        event = dict(type='member-verdict', spec=self.members[0], window='rh-prior',
                     project='demo', tree=self.row['tree'], state='red', nodes=[NODE],
                     required=self.row['required'], ts=current.isoformat())
        for changes, calls in (({},0), (dict(tree='a'*40),1),
                               (dict(ts=(current-timedelta(days=2)).isoformat()),1),
                               (dict(project='foreign'),1), (dict(required=[]),1),
                               (dict(nodes=[OTHER]),1), (dict(nodes=[{}]),1)):
            with self.subTest(changes=changes), patch.dict(os.environ,self.env), \
                 patch.object(w.fold,'ROOT',self.root), patch.object(w.fold,'EVENTS',self.events):
                events = w.admitted_events()+[dict(event,**changes)]
                entries = w.gate.resolve_entries(self.repo,self.members,events)
                with patch.object(w.attribution,'probe',return_value=dict(states={NODE:'undetermined'})) as probe:
                    result,proof = w.diagnosis(self.repo,self.row,entries,events,[NODE],'master')
                self.assertEqual(probe.call_count,calls)
                self.assertEqual(result['culprits'],self.members if not calls else [])
                print('reuse matrix',changes,'dispatches',probe.call_count,result)

    def test_existing_proven_pair_overrides_clean_singles_but_fallback_never_blames(self):
        import window_bisect as w
        self.members = self.members[:2]
        self.red()
        with patch.dict(os.environ,self.env), patch.object(w.fold,'ROOT',self.root), \
             patch.object(w.fold,'EVENTS',self.events):
            events = w.admitted_events()
            entries = w.gate.resolve_entries(self.repo,self.members,events)
            for mode,proven in (('pairs',{self.members[1]:[NODE]}),('newest-half',{})):
                row=dict(self.row,attribution=dict(mode=mode,proven=proven,drop={self.members[1]:[NODE]}))
                with patch.object(w.attribution,'probe',return_value=dict(states={NODE:'green'})):
                    result,proof = w.diagnosis(self.repo,row,entries,events,[NODE],'master')
                self.assertEqual(proof,proven)
                self.assertEqual(result['culprits'],list(proven))
                if proven:
                    row['ts']='2000-01-01T00:00:00+00:00'
                    with patch.object(w.attribution,'probe',return_value=dict(states={NODE:'green'})):
                        result,proof=w.diagnosis(self.repo,row,entries,events,[NODE],'master')
                    self.assertFalse(proof)
                    self.assertFalse(result['culprits'])

    def test_unknown_transport_does_not_split_or_disguise_uncertainty(self):
        import window_bisect as w
        for answer in (None, {}, {NODE:'undetermined'}, {NODE:[]}, {OTHER:'green'}):
            calls=[]
            result,proof=w.split_diagnosis(self.members,[NODE],lambda g,d:calls.append(g) or answer)
            self.assertEqual(calls,[self.members])
            self.assertEqual(result,dict(culprits=[],innocent=[],requeued=self.members,depth=0))
            self.assertFalse(proof)

    def test_depth_cap_never_blames_a_half(self):
        import window_bisect as wb
        for size in (1, 2, 4, 6):
            members = ['L-spec-' + str(9001 + i) for i in range(size)]
            visits = []
            def judge(group, depth):
                visits.append((group, depth))
                return {NODE: 'red'}
            result, proven = wb.split_diagnosis(members, [NODE], judge)
            self.assertLessEqual(result['depth'], 2)
            self.assertEqual(result['culprits'], [s for group, _ in visits if len(group) == 1 for s in group])
            self.assertEqual(result['requeued'], [s for s in members if s not in proven])
            print('bounded trace', size, visits, result)
            receipt('bounded-depth-matrix', dict(size=size, visits=visits, result=result))

    def test_two_culprits_green_siblings_and_partial_evidence(self):
        import window_bisect as wb
        members = ['L-spec-' + str(9001 + i) for i in range(4)]
        def judge(group, depth):
            return {NODE: 'red' if members[0] in group else 'green',
                    OTHER: 'red' if members[3] in group else 'green'}
        result, proven = wb.split_diagnosis(members, [NODE, OTHER], judge)
        self.assertEqual(result['culprits'], [members[0], members[3]])
        self.assertEqual(result['innocent'], members[1:3])
        self.assertEqual(proven, {members[0]: [NODE], members[3]: [OTHER]})
        receipt('independent-culprits', dict(result=result, proven=proven))


class RetryTests(Fixture):
    def test_consumer_restart_recovers_reserved_group_without_new_batch(self):
        self.success(self.cli())
        request = self.requests()[0]
        crashed = self.consumer(request, 'rh-reserved', 124, interrupt=True)
        self.assertEqual(crashed.returncode, 4, crashed.stderr)
        self.assertEqual(self.requests()[0]['state'], 'claimed')
        self.assertFalse((self.queue / '124.batch').exists())
        self.assertEqual(self.launch_predicate(self.queue / '124.batch'), 'pending')
        claimed = self.consume(request, 'rh-would-duplicate', 125)
        self.assertEqual((claimed['claimed_rehearsal'], claimed['claimed_batch']),
                         ('rh-reserved', '124.batch'))
        self.assertFalse((self.queue / '125.batch').exists())
        self.record('undetermined')
        self.success(self.cli())
        singles = [r for r in self.requests() if r['kind'] == 'singleton']
        self.assertEqual([r['members'] for r in singles], [[s] for s in self.members])

    def test_restart_new_ids_whole_then_singletons_and_singleton_stays_single(self):
        self.success(self.cli())
        root = self.requests()[0]
        self.consume(root, 'rh-whole-retry', 124)
        self.record('undetermined')
        self.success(self.cli())
        singles = [r for r in self.requests() if r['kind']=='singleton']
        self.assertEqual([r['members'] for r in singles], [[s] for s in self.members])
        self.assertTrue(all(r['attempt']==2 and r['lineage']==root['lineage'] for r in singles))
        self.consume(singles[0], 'rh-single-retry', 125)
        self.record('undetermined')
        self.success(self.cli())
        request = next(r for r in self.requests() if r['source_rehearsal']==self.ident)
        self.assertEqual((request['kind'],request['attempt'],request['lineage']), ('singleton',2,root['lineage']))

    def test_refused_preserves_attempts_one_and_two(self):
        self.success(self.cli())
        self.consume(self.requests()[0], 'rh-refused-one', 124)
        self.record('refused', reason='rehearsal-cap')
        self.success(self.cli())
        deferred = next(r for r in self.requests() if r['kind']=='deferred')
        self.assertEqual(deferred['attempt'], 1)
        self.consume(deferred, 'rh-uncertain-again', 125)
        self.record('undetermined')
        self.success(self.cli())
        single = next(r for r in self.requests() if r['kind']=='singleton')
        self.consume(single, 'rh-refused-two', 126)
        self.record('refused', reason='rehearsal-cap')
        self.success(self.cli())
        self.assertEqual(next(r for r in self.requests() if r['source_rehearsal']==self.ident)['attempt'],2)

    def test_initial_uncertain_singleton_stays_singleton(self):
        self.members = self.members[:1]
        self.record('undetermined')
        self.success(self.cli())
        self.assertEqual([(r['kind'],r['attempt']) for r in self.requests()], [('singleton',2)])

    def test_whole_retry_replays_without_duplicate_requests(self):
        first = self.success(self.cli())
        rows = self.requests()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['members'], self.members)
        self.assertEqual((rows[0]['kind'], rows[0]['attempt']), ('whole', 1))
        self.assertEqual(self.success(self.cli()), first)
        self.assertEqual(self.requests(), rows)

    def test_refused_does_not_consume_uncertainty(self):
        self.record('refused', reason='rehearsal-cap')
        self.success(self.cli())
        self.assertEqual([(r['kind'], r['attempt']) for r in self.requests()], [('deferred', 0)])

    def test_missing_acknowledgement_refuses_without_mutation(self):
        (self.queue / 'bisect-protocol.json').unlink()
        self.assertNotEqual(self.cli().returncode, 0)
        self.assertTrue(self.batch.exists())
        self.assertFalse(self.requests())


class PersistenceTests(Fixture):
    def test_competing_cli_invocations_commit_one_disposition(self):
        args=['bash',str(SRC.parent/'doit'),'window-bisect',self.ident,'--project','demo',
              '--specs',','.join(self.members),'--batch',str(self.batch),'--failed-ids',str(self.idsfile)]
        with subprocess.Popen(args,env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as first, \
             subprocess.Popen(args,env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as second:
            outputs=[]
            for proc in (first,second):
                stdout,stderr=proc.communicate(timeout=30)
                self.assertEqual(proc.returncode,0,stderr)
                outputs.append(json.loads(stdout))
        self.assertEqual(outputs[0],outputs[1])
        self.assertEqual(len(self.requests()),1)
        self.assertEqual(len([r for r in self.ledger() if r['type']=='batch-member-released']),len(self.members))

    def test_partial_diagnosis_commits_green_siblings_and_singleton_residuals(self):
        self.red()
        script=self.transport(before='''
def partial(repo,sha,ids):
 files=w.gate.git(repo,'ls-tree','--name-only',sha).stdout.splitlines()
 count=sum(s+'.txt' in files for s in '''+repr(self.members)+''')
 state='red' if count==4 else 'undetermined' if 'L-spec-9001.txt' in files else 'green'
 return dict(sha=sha,states=dict.fromkeys(ids,state))
w.attribution.probe=partial
''')
        result=self.success(self.cli(script=script))
        self.assertEqual(result['innocent'],self.members[2:])
        self.assertEqual(result['requeued'],self.members[:2])
        self.assertFalse(result['culprits'])
        self.assertEqual([r['members'] for r in self.requests()],[[s] for s in self.members[:2]])
        self.assertTrue(all(r['attempt']==2 for r in self.requests()))

    def test_determined_red_parks_only_positive_members_with_fresh_reader(self):
        self.red()
        self.env['DOIT_ROLE'] = 'thinker'
        result = self.success(self.cli(script=self.transport(failing=self.members[:1])))
        self.assertEqual(result['culprits'], self.members[:1])
        self.assertEqual(result['innocent'], self.members[1:])
        rows = self.ledger()
        parks = [r for r in rows if r['type']=='batch-member-parked']
        self.assertEqual([(r['spec'],r['ids'],r['attributed'],r['actor']) for r in parks],
                         [(self.members[0],[NODE],True,'executor')])
        self.assertIn('9001 ', (self.queue/'attention.txt').read_text())
        self.assertEqual(self.ready.read_text().splitlines(), [s[7:] for s in self.members[1:]])
        state=json.loads(next((self.queue/'bisect-state').glob('*.json')).read_text())
        source=state['sources'][self.ident]
        self.assertEqual(source['retirement_count'],2)
        self.assertTrue(source['complete'])
        releases = [r for r in rows if r['type'] == 'batch-member-released']
        self.assertEqual([r['spec'] for r in releases], self.members[1:])
        self.assertTrue(all(r['project'] == 'demo' and r['batch'] == self.ident
                            and r['lineage'] == state['lineage'] for r in parks + releases))
        receipt('fresh-reader', dict(events=parks + releases,
                                    retirement_count=source['retirement_count']))

    def test_failure_after_each_persistence_stage_recovers_once(self):
        self.red()
        script = self.transport(failing=self.members[:1], before='''
original=w.atomic
count=0
def fail(path,content):
 global count
 original(path,content)
 count+=1
 if count==int(w.os.environ['FAIL_AT']): raise OSError('injected write failure')
w.atomic=fail
''')
        for stage in (1,2,3,4,5,6):
            # Each new source has its own retained preimages; never replace the ledger.
            self.env['FAIL_AT'] = str(stage)
            proc = self.cli(script=script)
            if proc.returncode == 0:
                break
            self.assertNotEqual(proc.returncode, 0)
            self.success(self.cli(script=self.transport(failing=self.members[:1])))
            self.assertEqual(len([r for r in self.ledger() if r['type']=='batch-member-parked']),1)
            # Reset only the transaction artifacts and restore the original source for next injection.
            import shutil
            shutil.rmtree(self.queue/'bisect-state')
            self.ready.unlink(missing_ok=True)
            (self.queue/'attention.txt').unlink(missing_ok=True)
            self.batch.write_text(self.base+' '+' '.join(s[7:] for s in self.members)+'\n')
            Path(str(self.batch)+'.ci').write_text('exit=2\n')

    def test_shared_lock_preserves_append_before_and_during_rewrite(self):
        self.red()
        attention = self.queue/'attention.txt'
        append_before='exec 9>"$1/.bisect.lock"\nflock -x 9\nprintf "%s\\n" "9998 00:01 manual before lock" >> "$1/attention.txt"'
        subprocess.run(['bash','-c',append_before,'writer',str(self.queue)],check=True,env=self.env)
        entered, release = self.root/'entered', self.root/'release'
        script = self.transport(failing=self.members[:1], before='''
original=w.atomic
def paused(path,content):
 if path.name=='attention.txt':
  (w.fold.ROOT/'entered').touch()
  import time
  until=time.monotonic()+15
  while not (w.fold.ROOT/'release').exists():
   if time.monotonic()>until: raise TimeoutError('writer fixture')
   time.sleep(.01)
 original(path,content)
w.atomic=paused
''')
        args=[self.ident,'--project','demo','--specs',','.join(self.members),'--batch',str(self.batch),'--failed-ids',str(self.idsfile)]
        with subprocess.Popen([sys.executable,'-c',script,str(SRC),*args], env=self.env,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as proc:
            import time
            until=time.monotonic()+20
            while not entered.exists() and proc.poll() is None and time.monotonic()<until:
                time.sleep(.01)
            self.assertTrue(entered.exists())
            writer='touch "$2"\nexec 9>"$1/.bisect.lock"\nflock -x 9\nprintf "%s\\n" "9999 00:02 manual during rewrite" >> "$1/attention.txt"'
            started=self.root/'append-started'
            with subprocess.Popen(['bash','-c',writer,'writer',str(self.queue),str(started)],env=self.env) as append:
                until=time.monotonic()+10
                while not started.exists() and time.monotonic()<until:
                    time.sleep(.01)
                self.assertTrue(started.exists())
                self.assertIsNone(append.poll())
                release.touch()
                stdout,stderr=proc.communicate(timeout=25)
                self.assertEqual(proc.returncode,0,stderr)
                append.wait(timeout=20)
                self.assertEqual(append.returncode,0)
        reader=subprocess.run([sys.executable,'-c','import sys;print(open(sys.argv[1]).read())',str(attention)],capture_output=True,text=True)
        self.assertIn('9998 00:01 manual before lock',reader.stdout)
        self.assertIn('9999 00:02 manual during rewrite',reader.stdout)
        self.assertEqual(len([r for r in self.ledger() if r['type']=='batch-member-parked']),1)

    def test_real_ledger_readback_preserves_foreign_holds(self):
        attention = self.queue / 'attention.txt'
        legacy = b'9001 01:02 unattributed\n9002 01:03 manual hold\n'
        attention.write_bytes(legacy)
        self.success(self.cli())
        self.assertEqual(attention.read_bytes(), legacy)
        rows = [e for e in self.ledger() if e['type'] == 'batch-member-released']
        self.assertEqual([e['spec'] for e in rows], self.members)
        self.assertTrue(all(e['actor'] == 'executor' and e['batch'] == self.ident for e in rows))
        self.assertFalse([e for e in self.ledger() if e['type'] == 'batch-member-parked'])
        self.success(self.cli())
        self.assertEqual(len([e for e in self.ledger() if e['type'] == 'batch-member-released']), 4)


class SafetyTests(Fixture):
    def test_invalid_recorded_attribution_is_rejected_before_transport(self):
        self.red()
        ledger=self.events/'L-executor-fixture.jsonl'
        original=ledger.read_text().splitlines()
        for proof in ({'L-spec-9999':[NODE]}, {self.members[0]:[OTHER]},
                      {self.members[0]:[NODE,NODE]}, []):
            row=dict(self.row,attribution=dict(mode='pairs',proven=proof))
            ledger.write_text('\n'.join(original[:-1]+[json.dumps(row)])+'\n')
            proc=self.cli(script=self.transport(before='w.base_red.classify_selected=forbidden;w.attribution.probe=forbidden'))
            self.assertEqual(proc.returncode,2,proc.stderr)
            self.assertNotIn('unexpected live transport',proc.stderr)
            self.assertTrue(self.batch.exists())
            self.assertFalse(self.requests())

    def test_hostile_identities_and_selectors_do_not_mutate(self):
        cases = [(['--project',v]) for v in ('../demo','-demo','demo\nforeign')]
        cases += [['--specs',v] for v in ('L-spec-../9001','L-spec-09001','-9001','9001','L-spec-9001\n')]
        for extra in cases:
            self.assertNotEqual(self.cli(extra).returncode,0)
            self.assertFalse(self.requests())
        for ids in (['../bad.py::test'],['-flag.py::test'],['api/test.py::bad\nline'],[NODE,NODE]):
            self.idsfile.write_text(json.dumps(ids))
            self.assertNotEqual(self.cli().returncode,0)
            self.assertFalse(self.requests())

    def test_malformed_sidecar_and_symlinked_retry_path_refuse(self):
        sidecar=self.queue/'123.batch.lineage.json'
        digest=hashlib.sha256(json.dumps(['demo',self.ident,self.members],separators=(',',':')).encode()).hexdigest()
        valid=dict(version=1,request_id=None,project='demo',members=self.members,rehearsal=self.ident,
                   root_rehearsal=self.ident,lineage=digest,attempt=0)
        for value in ({},dict(valid,request_id='../escape'),dict(valid,version=True),
                      {k:v for k,v in valid.items() if k!='request_id'}):
            sidecar.write_text(json.dumps(value))
            self.assertNotEqual(self.cli().returncode,0)
            self.assertFalse(self.requests())
        sidecar.unlink()
        outside=self.root/'outside'
        outside.mkdir()
        (self.queue/'bisect-retry').symlink_to(outside,target_is_directory=True)
        self.assertNotEqual(self.cli().returncode,0)
        self.assertFalse(list(outside.iterdir()))

    def test_tampered_journal_cannot_override_queue_paths_or_forge_holds(self):
        self.success(self.cli())
        statefile=next((self.queue/'bisect-state').glob('*.json'))
        original=json.loads(statefile.read_text())
        for kind in ('path','ownership','members'):
            state=json.loads(json.dumps(original))
            source=state['sources'][self.ident]
            if kind=='path': source['plan']['requests'][0]['request_id']='../bisect-protocol'
            if kind=='ownership': source['attention_rows']={self.members[0]:'9001 01:00 manual forged\n'}
            if kind=='members': source['plan']['result']['requeued']=self.members[:1]
            statefile.write_text(json.dumps(state))
            before=(self.queue/'bisect-protocol.json').read_bytes()
            self.assertNotEqual(self.cli().returncode,0)
            self.assertEqual((self.queue/'bisect-protocol.json').read_bytes(),before)

    def test_conflicting_terminal_evidence_and_raw_diagnostics_are_refused(self):
        self.red()
        ledger=self.events/'L-executor-fixture.jsonl'
        with ledger.open('a') as stream:
            stream.write(json.dumps(dict(self.row,required=['different']))+'\n')
        proc=self.cli(script=self.transport(before="w.base_red.classify_selected=forbidden"))
        self.assertNotEqual(proc.returncode,0)
        self.assertFalse(self.requests())
        # A diagnostic exception's raw bytes must never reach either output stream.
        self.record('undetermined')
        script=self.transport(before="w.load_source=lambda *a: (_ for _ in ()).throw(OSError('PRIVATE_DIAGNOSTIC_SENTINEL'))")
        proc=self.cli(script=script)
        self.assertNotEqual(proc.returncode,0)
        self.assertNotIn('PRIVATE_DIAGNOSTIC_SENTINEL',proc.stdout+proc.stderr)

    def test_paths_and_symlink_escapes_cost_no_dispatch(self):
        outside = self.root / 'outside.batch.red'
        outside.write_text('sentinel')
        for path in (str(outside), '../outside.batch.red', str(self.queue / '-1.batch.red')):
            self.assertNotEqual(self.cli(['--batch', path]).returncode, 0)
            self.assertEqual(outside.read_text(), 'sentinel')
        self.batch.unlink()
        self.batch.symlink_to(outside)
        self.assertNotEqual(self.cli().returncode, 0)
        self.assertEqual(outside.read_text(), 'sentinel')

    def test_malformed_and_oversized_failed_id_files(self):
        for content in ('{}', 'null', '[1]', '[""]', '[' + ' ' * 65000 + ']'):
            self.idsfile.write_text(content)
            self.assertNotEqual(self.cli().returncode, 0)
            self.assertFalse(self.requests())


class AcceptanceResult(unittest.TextTestResult):
    """Retain the successful case matrix for each acceptance receipt."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = {}

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.setdefault(type(test).__name__, []).append(test._testMethodName)


if __name__ == '__main__':
    runner = unittest.TextTestRunner(verbosity=2, resultclass=AcceptanceResult)
    result = unittest.main(testRunner=runner, exit=False).result
    classes = ('CliTests', 'BaseClassificationTests', 'BoundedDiagnosisTests',
               'RetryTests', 'PersistenceTests', 'SafetyTests')
    failed = {type(test).__name__ for test, _ in result.failures + result.errors}
    failed.update(type(test).__name__ for test, _ in result.skipped)
    for index, name in enumerate(classes, 1):
        cases = result.passed.get(name, [])
        if cases and name not in failed and result.wasSuccessful():
            receipt(f'AC{index}', dict(result='ok', cases=cases))
            print(f'AC{index} ok: {len(cases)} cases')
    sys.exit(0 if result.wasSuccessful() else 1)
