"""Pre-grade checks are advisory unless exact build and master prove a failure."""
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
import io
import tempfile
import unittest
from unittest.mock import Mock, patch

import dispatch
import fold
import fold_emits
import lane_rules
import nextstep
import precheck as pc
import precheck_targeted as pt

NOW=datetime(2026,10,30,12,tzinfo=timezone.utc)


class PregradeTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(fold,'ROOT',self.root))
        self.stack.enter_context(patch.object(pc.merge_queue,'_repository',return_value=(self.root,'main')))
        self.stack.enter_context(patch.object(pc,'master_sha',return_value='master'))
        self.merges=self.stack.enter_context(patch.object(pc,'merge',side_effect=lambda repo,master,sha:('solo-'+sha,None)))
        self.stack.enter_context(patch.object(pc,'check_tree',side_effect=lambda repo,commit,runner:runner(Path('/trees')/commit)))
        self.events=[]; self.calls=[]
        self.guards=Mock(return_value=[])
        self.target=self.stack.enter_context(patch.object(pt,'run',side_effect=self.targeted))

    def build(self,sid='L-spec-9001',sha='a1',minute=-10):
        for kind,fields in [('spec-written',{}),('spawn-done',dict(actor='spec-auditor',bad_cut=False)),('build-done',dict(ready_sha=sha,status='DONE',actor='builder'))]:
            self.events.append(dict(type=kind,subject=sid,ts=(NOW+timedelta(minutes=minute)).isoformat(),actor='operator',project='proj',_src=f'L-operator-fixture.jsonl:{len(self.events)+1}')|fields)

    def emit(self,sid,kind,**fields):
        event=dict(type=kind,subject=sid,actor='executor',project='proj',ts=NOW.isoformat(),_src=f'L-executor-fixture.jsonl:{len(self.events)+1}',**fields)
        self.events.append(event); return event

    def targeted(self,repo,master,clean,shas,previous,content,**kw):
        self.calls.append((clean,shas,kw))
        return dict(targeted='green',targeted_red={},targeted_unscanned={},targeted_unattributed={})

    def run_pass(self,**kw):
        return pc.pregrade('proj',events=self.events,emit=self.emit,guard_runner=self.guards,now=NOW,**kw)

    def rows(self,kind): return [e for e in self.events if e['type']==kind]

    def test_pregrade_conflict_and_clean(self):
        self.build(); self.build('L-spec-9002','b1',-9)
        self.merges.side_effect=lambda repo,master,sha: (None,'x.py') if sha=='b1' else ('solo-'+sha,None)
        self.run_pass()
        self.assertEqual([e['verdict'] for e in self.rows('precheck-pregrade')],['clean','conflict'])
        conflict=self.rows('merge-conflict')[0]
        self.assertEqual((conflict['source'],conflict['path'],conflict['branch_sha']),('precheck-pregrade','x.py','b1'))
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][0],[{'spec':'L-spec-9001','branch':'a1'}])
        self.guards.assert_called_once_with(Path('/trees/solo-a1'))

    def test_pregrade_red_only_when_new(self):
        self.build()
        node='tests/test_a.py::t'
        self.target.side_effect=lambda *a,**k:dict(targeted='red',targeted_red={'L-spec-9001':[node]},targeted_unscanned={},targeted_unattributed={})
        self.run_pass()
        self.assertEqual(self.rows('precheck-pregrade')[0]['verdict'],'red')
        self.assertEqual((self.rows('precheck-red')[0]['layer'],self.rows('precheck-red')[0]['tests']),('pregrade',[node]))
        # Guard comparison independently excludes the same baseline failure.
        self.events=[e for e in self.events if e['type'] not in {'precheck-pregrade','precheck-red'}]
        self.target.side_effect=self.targeted; self.guards.return_value=[node]
        self.run_pass()
        self.assertEqual(self.rows('precheck-pregrade')[0]['verdict'],'clean')
        self.assertEqual(self.rows('precheck-red'),[])

    def test_pregrade_once_per_sha(self):
        self.build(); self.run_pass(); self.run_pass()
        self.assertEqual(len(self.rows('precheck-pregrade')),1)
        self.build(sha='a2',minute=-1); self.run_pass()
        self.assertEqual(len(self.rows('precheck-pregrade')),2)

    def test_pregrade_cap_oldest_first(self):
        for n in reversed(range(12)): self.build(f'L-spec-{9000+n}',f'a{n}',-20+n)
        self.run_pass()
        self.assertEqual([e['branch_sha'] for e in self.rows('precheck-pregrade')],['a0','a1','a2','a3','a4'])

    def test_pregrade_timeout_undetermined(self):
        for failure in ('runner','scan'):
            with self.subTest(failure=failure):
                self.events=[]; self.build()
                self.guards.side_effect=TimeoutError() if failure=='runner' else None
                self.target.side_effect=RuntimeError('scan') if failure=='scan' else self.targeted
                self.run_pass()
                self.assertEqual(self.rows('precheck-pregrade')[0]['verdict'],'undetermined')
                self.assertEqual(self.rows('precheck-red'),[])

    def test_pregrade_lock_skips(self):
        self.build()
        with pc.pregrade_lock() as owned:
            self.assertTrue(owned); self.assertEqual(self.run_pass(),[])
        self.assertEqual(self.rows('precheck-pregrade'),[])
        with pc.lock('proj') as owned:
            self.assertTrue(owned); self.run_pass() # independent queue lock
        self.assertEqual(len(self.rows('precheck-pregrade')),1)

    def test_refusal_matrix(self):
        self.build()
        self.events.append(dict(type='evidence-bundle-failed',subject='L-spec-9001',reason='wall cap exceeded'))
        self.assertIsNone(pc.pregrade_refusal(self.events,'L-spec-9001','master'))
        with patch.object(pc, 'master_sha', side_effect=AssertionError('no check result; no git probe')):
            self.assertIsNone(pc.grader_master(self.events,'L-spec-9001','proj'))
        self.assertIsNone(pc.pregrade_refusal(self.events,'L-spec-9001','master'))
        for verdict in ('clean','undetermined','red','conflict'):
            self.emit('L-spec-9001','precheck-pregrade',branch_sha='a1',master='master',verdict=verdict,tests=['tests/a.py::x'],path='x.py')
            answer=pc.pregrade_refusal(self.events,'L-spec-9001','master')
            if verdict in ('red','conflict'): self.assertIn('tests/a.py::x' if verdict=='red' else 'x.py',answer)
            else: self.assertIsNone(answer)
            self.assertIsNone(pc.pregrade_refusal(self.events,'L-spec-9001','moved'))
        self.build(sha='a2',minute=-1)
        self.assertIsNone(pc.pregrade_refusal(self.events,'L-spec-9001','master'))

    def test_dispatch_wiring_and_size(self):
        source=Path(dispatch.__file__).read_text()
        self.assertLess(source.index('COMMIT-SHAPE stands'),source.index('precheck.pregrade_refusal'))
        self.assertIn('reason="precheck-red"',source)
        self.assertLessEqual(len(source.splitlines()),1816)

    def test_emits_and_reuse(self):
        self.assertEqual(fold_emits.EMITS['precheck-pregrade'],{'tick','executor','operator'})
        self.assertEqual(fold_emits.REQUIRED_ROWS['precheck-pregrade'],('master','branch_sha','verdict'))

    def test_nextstep_routes_pregrade_red(self):
        self.build()
        self.emit('L-spec-9001','precheck-pregrade',branch_sha='a1',master='master',verdict='red')
        self.emit('L-spec-9001','precheck-red',branch_sha='a1',master='master',layer='pregrade',tests=['tests/a.py::x'])
        def step():
            return next(s for s in nextstep.next_steps(self.events,{'L-spec-9001':{'state':'building'}},NOW,lane_rules.load()) if s.item=='L-spec-9001')
        s=step(); self.assertEqual(s.action,'rebuild'); self.assertIn('tests/a.py::x',s.reason)
        self.build(sha='a2',minute=1)
        self.assertNotEqual(step().action,'rebuild')

    def test_stats_line(self):
        self.events=[dict(type='precheck-pregrade',ts=NOW.isoformat(),verdict=v) for v in ('clean','conflict','red')]
        self.events += [dict(type='window-run-minutes',ts=NOW.isoformat(),window=w,state='red') for w in ('w1','w1','w2')]
        out=io.StringIO()
        with patch.object(pc.window_gate,'train_events',return_value=self.events),redirect_stdout(out):
            self.assertEqual(pc.main(['--stats','--since','2026-10-29']),0)
        self.assertEqual(out.getvalue().strip(),'2026-10-30 clean=1 conflict=1 red=1 undetermined=0 red_windows=2')


if __name__=='__main__': unittest.main()
