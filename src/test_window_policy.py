"""Window defaults and ledger-replayed AIMD, using isolated actor files."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

_TMP = tempfile.TemporaryDirectory(prefix='window-policy-import-')
os.environ['DOIT_ROOT'] = _TMP.name
os.environ['DOIT_NO_POKE'] = '1'
import fold
import lane_rules
import merge_queue
import nextstep
import tick
import window_gate

NOW = datetime(2026, 11, 1, 20, tzinfo=timezone.utc)
WIN = 'win-default'
PROJECT = 'albert-scott'
A, B = 'L-spec-9001', 'L-spec-9002'
NODE = 'tests/test_clock.py::test_boundary'


class WindowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='window-policy-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for obj, key, value in [(fold,'ROOT',self.root),(fold,'EVENTS',self.root/'events'),(fold,'NOW',NOW)]:
            p=patch.object(obj,key,value);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,DOIT_ROOT=str(self.root),DOIT_LEDGER_FILE='L-operator-fixture.jsonl',DOIT_NO_POKE='1')
        p.start();self.addCleanup(p.stop)
        p=patch('dispatch.now',return_value=NOW.isoformat());p.start();self.addCleanup(p.stop)
        self.events=[]

    def add(self, kind, subject=WIN, minutes=-31, **fields):
        event=dict(type=kind,subject=subject,project=PROJECT,
                   ts=(NOW+timedelta(minutes=minutes)).isoformat());event.update(fields)
        actor='executor' if kind in ('queue-batch','shipped','window-opened') else 'operator'
        path=self.root/('events/L-'+actor+'-fixture.jsonl');path.parent.mkdir(exist_ok=True)
        with path.open('a') as f:f.write(json.dumps(event)+'\n')
        return fold.read_events()[-1]

    def seed(self, minutes=-30):
        for spec in [A,B]:self.add('queue-add',spec,minutes=-33,branch=spec.lower(),rule='enqueue')
        self.add('queue-batch',minutes=-32,specs=A+','+B)
        self.add('window-opened',specs=[A,B])
        self.add('window-tree-verified',window=WIN,sha='a'*40,tree='b'*40,green=False,new_ids=[NODE])
        return self.add('escalation-blocking',minutes=minutes,why='unattributed red',
                        default='hold',deadline=(NOW+timedelta(days=1)).isoformat(),revert='retry')

    def steps(self):
        return nextstep.next_steps(fold.read_events(),{},NOW,lane_rules.load())

    def test_timeout_boundary_and_structured_exact_ref(self):
        esc=self.seed(-29)
        self.assertEqual(next(s for s in self.steps() if s.item==WIN).state,'hold')
        with patch.object(fold,'NOW',NOW):
            self.add('decision','other',ref=esc['_src'],ruling={'kind':'base-red-accepted','nodes':[NODE]})
        self.assertFalse(any(s.action=='window-default' for s in self.steps()))

    def test_legacy_prose_does_not_stop_expired_default(self):
        esc=self.seed()
        self.add('decision',why='please continue',ref=esc['_src'])
        step=next(s for s in self.steps() if s.item==WIN)
        self.assertEqual((step.state,step.action,step.owner),('action','window-default','tick'))

    def test_tick_default_requeues_unattributed_and_frees_lane_idempotently(self):
        esc=self.seed()
        self.add('window-dropped',A,window=WIN,nodes=[NODE],owned_nodes=[NODE],reason='owner')
        steps=self.steps()
        with patch.object(tick,'_record_held_skips'),patch('loadgate.begin',return_value=None):
            output=tick.run_steps(fold.read_events(),{},NOW,steps=steps)
        self.assertTrue(any('window-default' in line and 'acted' in line for line in output),output)
        events=fold.read_events();rows=merge_queue.queue(events)
        self.assertEqual([(r['spec'],r['batch']) for r in rows],[(B,None)])
        defaults=[e for e in events if e['type']=='window-dropped' and e.get('reason')=='escalation-default']
        self.assertEqual([(e['subject'],e['disposition']) for e in defaults],[(B,'requeue')])
        self.assertEqual(defaults[0]['nodes'],[NODE])
        self.assertEqual(events[-1]['type'],'window-cancelled')
        self.assertIn(esc['_src'],fold.answered(events))
        self.assertFalse(fold.open_escalations(events))
        import fold_stalls
        by={}
        for e in events:by.setdefault(e['subject'],[]).append(e)
        self.assertFalse(fold_stalls.prepare(by,NOW)['windows'])
        count=len(events)
        self.assertFalse(any(s.item==WIN for s in self.steps()))
        import window_policy
        self.assertFalse(window_policy.apply_default(WIN,esc['_src'],NOW))
        self.assertEqual(len(fold.read_events()),count)
        self.assertEqual(merge_queue.next_candidate(fold.read_events()),B)

    def test_dry_run_and_resume_after_partial_default(self):
        esc=self.seed()
        self.add('window-dropped',A,minutes=-29,window=WIN,nodes=[NODE],reason='escalation-default',
                 disposition='requeue',ref=esc['_src'])
        import window_policy
        before=len(fold.read_events())
        files=set(self.root.rglob('*'))
        self.assertTrue(window_policy.apply_default(WIN,esc['_src'],NOW,dry_run=True))
        self.assertEqual(len(fold.read_events()),before)
        self.assertEqual(set(self.root.rglob('*')),files)
        self.assertTrue(window_policy.apply_default(WIN,esc['_src'],NOW))
        self.assertCountEqual([r['spec'] for r in merge_queue.queue(fold.read_events())],[A,B])
        self.assertEqual(len([e for e in fold.read_events() if e['type']=='window-dropped' and e['subject']==A]),1)

    def test_append_refuses_prose_or_invalid_window_ruling_before_write(self):
        esc=self.seed()
        for value in [None,{}, {'kind':'unknown'}, {'kind':'base-red-accepted','nodes':[]}]:
            args=['decision',WIN,'ref='+esc['_src'],'why=continue']
            if value is not None:args+=['ruling:='+json.dumps(value)]
            count=len(fold.read_events())
            with self.assertRaisesRegex(SystemExit,'ruling:='):
                fold.append(args,strict=True)
            self.assertEqual(len(fold.read_events()),count)
        fold.append(['decision',WIN,'ref='+esc['_src'],'ruling:='+json.dumps({'kind':'base-red-accepted','nodes':[NODE]})],strict=True)
        self.assertFalse(any(s.action=='window-default' for s in self.steps()))
        fold.append(['decision',A,'ref=ordinary-question','why=ordinary prose'],strict=True)

    def test_wrong_ref_and_unattributed_old_drop_do_not_stall_requeue(self):
        esc=self.seed()
        self.add('decision','other',ref='different:1',ruling={'kind':'base-red-accepted','nodes':[NODE]})
        self.add('window-dropped',A,minutes=-29,window=WIN,nodes=[NODE],reason='bisect')
        import window_policy
        self.assertTrue(window_policy.apply_default(WIN,esc['_src'],NOW))
        self.assertCountEqual([r['spec'] for r in merge_queue.queue(fold.read_events())],[A,B])

    def test_bisection_attribution_stays_dropped_without_owned_nodes(self):
        esc=self.seed()
        for spec,reason in [(A,'bisect-single'),(B,'bisect-owner')]:
            self.add('window-dropped',spec,window=WIN,nodes=[NODE],owned_nodes=[],reason=reason)
        import window_policy
        self.assertTrue(window_policy.apply_default(WIN,esc['_src'],NOW))
        self.assertEqual(merge_queue.queue(fold.read_events()),[])
        self.assertFalse(any(e['type']=='window-dropped' and e.get('reason')=='escalation-default'
                             for e in fold.read_events()))

    def test_default_preserves_shipped_specs_and_another_windows_reservation(self):
        esc=self.seed()
        self.add('shipped',A,minutes=-1,window='win-new',sha='a'*40)
        self.add('queue-batch','win-new',minutes=-1,specs=B)
        import window_policy
        self.assertTrue(window_policy.apply_default(WIN,esc['_src'],NOW))
        self.assertEqual([(r['spec'],r['batch']) for r in merge_queue.queue(fold.read_events())],[(B,'win-new')])
        self.assertFalse(any(e['type']=='window-dropped' and e.get('reason')=='escalation-default' for e in fold.read_events()))

    def test_gate_lock_prevents_default_during_a_running_attempt(self):
        esc=self.seed()
        with window_gate.chain_lock(WIN,PROJECT,root=self.root):
            import window_policy
            count=len(fold.read_events())
            self.assertFalse(window_policy.apply_default(WIN,esc['_src'],NOW))
            self.assertEqual(len(fold.read_events()),count)
        self.add('window-opened',minutes=-1,specs=[A,B])
        self.assertFalse(any(s.action=='window-default' for s in self.steps()))

    def test_aimd_restart_red_retry_green_and_override(self):
        self.assertEqual(window_gate.max_merges({}),4)
        def result(window,state,first=True,project=PROJECT):
            self.add('window-verification-result',window,state=state,first_run=first,project=project)
        result('win-a','green');self.assertEqual(window_gate.max_merges({}),4)
        result('win-b','green');self.assertEqual(window_gate.max_merges({}),5)
        # Replay from files rather than a process-local streak.
        import importlib
        import window_policy
        importlib.reload(window_policy)
        self.assertEqual(window_gate.max_merges({}),5)
        result('win-c','red');self.assertEqual(window_gate.max_merges({}),2)
        result('win-c','green');result('win-d','green')
        self.assertEqual(window_gate.max_merges({}),2)
        result('win-e','green');self.assertEqual(window_gate.max_merges({}),3)
        result('win-other','red',project='other');self.assertEqual(window_gate.max_merges({}),3)
        self.assertEqual(window_gate.max_merges({'DOIT_WINDOW_MAX_MERGES':'12'}),12)
        for i in range(30):result('win-grow-'+str(i),'green')
        self.assertEqual(window_gate.max_merges({}),8)
        for i in range(10):result('win-red-'+str(i),'red')
        self.assertEqual(window_gate.max_merges({}),2)


if __name__=='__main__':unittest.main()
