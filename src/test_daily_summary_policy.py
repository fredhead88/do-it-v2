"""Shared 0051/R9 + 0058/R3 delivery through the production commands, fake transport only."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_IMPORT = tempfile.TemporaryDirectory(prefix='summary-policy-import-')
os.environ['DOIT_ROOT'] = _IMPORT.name
import fold
import owed_digest
import stuck_digest
import thinker_inbox as inbox
import tg_say

NOW = datetime(2026,11,2,6,5,tzinfo=timezone.utc)
SID = 'L-spec-9001'


def event(event_type, hours, subject=SID, actor=None, **fields):
    actor=actor or {'owed-ac':'spec-writer','spec-written':'spec-writer','shipped':'executor',
                    'owed-unobservable':'owed-sweeper','decision':'operator'}.get(event_type,'executor')
    return dict(type=event_type,subject=subject,actor=actor,
                ts=(NOW-timedelta(hours=hours)).isoformat(), **fields)


def check(subject=SID, age=1, ask='Please approve the production change.', **fields):
    return [event('spec-written',100,subject),
            event('owed-ac',99,subject,criterion='AC1',wake_at=(NOW-timedelta(hours=90)).isoformat()),
            event('shipped',90,subject),
            event('owed-unobservable',age,subject,criterion='AC1',capability='operator-action',why=ask,**fields)]


class DailySummaryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='summary-policy-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'events').mkdir()
        for obj,name,value in [(fold,'ROOT',self.root),(fold,'EVENTS',self.root/'events'),(fold,'PROJECT',None)]:
            p=patch.object(obj,name,value);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,DOIT_ROOT=str(self.root),DOIT_NO_POKE='1');p.start();self.addCleanup(p.stop)
        (self.root/'state').mkdir()
        (self.root/'state/tg-cutover-done').touch()
        p=patch.object(tg_say,'_now',return_value=NOW);p.start();self.addCleanup(p.stop)
        self.sent=[]
        self.install(check())

    def install(self,events):
        for p in (self.root/'events').glob('*.jsonl'):p.unlink()
        for row in events:
            with (self.root/'events'/('L-'+row['actor']+'-fixture.jsonl')).open('a') as out:
                out.write(json.dumps({k:v for k,v in row.items() if k not in ('actor','_src')})+'\n')

    def sender(self,sender,text,*,label,root):
        self.assertEqual((sender,label,root),('daily-summary','[DAILY]',self.root))
        self.sent.append(text);return tg_say.Delivery('sent',200)

    def run_daily(self,first=owed_digest,now=NOW):
        for module in (first, stuck_digest if first is owed_digest else owed_digest):
            flags={'alert':True} if module is owed_digest else {'send':True}
            self.assertEqual(module.run(now=now,sender=self.sender,**flags),0)

    def test_both_command_orders_share_one_complete_durable_summary(self):
        self.run_daily()
        self.assertEqual(len(self.sent),1)
        text=self.sent[0]
        for part in ('Checks:','Work:','NEEDS YOU:','Recommend','No reply:','L-spec-9001/AC1'):
            self.assertIn(part,text)
        self.assertLessEqual(len(text),3500)
        self.run_daily(first=stuck_digest)
        self.assertEqual(len(self.sent),1)
        # Next day the reverse order still delivers once, and old delivered asks stay silent.
        self.run_daily(first=stuck_digest,now=NOW+timedelta(days=1))
        self.assertEqual(len(self.sent),2)
        self.assertNotIn('NEEDS YOU:',self.sent[-1])

    def test_current_exact_boundary_owner_and_capability_filters(self):
        ev=[]
        for name,age,ask,fields in [('9001',48,'Please approve the production change.',{}),
            ('9002',48.001,'Please approve the production change.',{}),
            ('9003',1,'Please approve the production change.',{'owner':'thinker'}),
            ('9004',1,'No browser, DSN or approved droplet wrapper is available.',{'owner':'operator'}),
            ('9005',1,'permission denied for relation public.fixture',{'owner':'operator'})]:
            ev+=check('L-spec-'+name,age,ask,**fields)
        self.install(ev);self.run_daily(first=stuck_digest)
        self.assertEqual(len(self.sent),1)
        self.assertIn('L-spec-9001/AC1',self.sent[0])
        for name in ('9002','9003','9004','9005'):
            self.assertNotIn('L-spec-'+name+'/',self.sent[0])
        self.assertIn('5 cannot run automatically yet',self.sent[0])
        record=json.loads((self.root/'state/telegram-summary.json').read_text())
        self.assertGreaterEqual(len(record['rows']),5)

    def test_report_column_is_not_a_real_world_purchase_request(self):
        self.install(check(ask='Reproduction by SELECT purchase_date, repurchase_cost from the read-only reporting view; operator runs the reader.'))
        self.run_daily()
        self.assertEqual(len(self.sent),1)
        self.assertNotIn('NEEDS YOU:',self.sent[0])
        self.assertIn('1 cannot run automatically yet',self.sent[0])

    def test_later_authorized_ruling_or_verdict_suppresses_both_views(self):
        for verdict,actor,fields in [('decision','operator',{'why':'Keep paused'}),
                                      ('owed-met','owed-sweeper',{'evidence':'observed result'}),
                                      ('owed-failed','owed-sweeper',{'evidence':'actual mismatch','kind':'unmet'})]:
            ev=check()+[event(verdict,.5,actor=actor,criterion='AC1',**fields)]
            self.install(ev)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(stuck_digest.run(now=NOW,dry_run=True),0)
            self.assertNotIn('NEEDS YOU:',json.loads(output.getvalue())['telegrams'][0])
        # An unauthorized verdict cannot hide the real request.
        self.install(check()+[event('owed-met',.5,actor='builder',criterion='AC1',evidence='not authorized')])
        self.run_daily()
        self.assertIn('NEEDS YOU:',self.sent[0])

    def test_failed_send_does_not_claim_the_day_other_command_can_retry(self):
        fail=lambda *a,**kw:tg_say.Delivery('failed',429)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(owed_digest.run(now=NOW,alert=True,sender=fail),1)
        self.assertNotIn('sent_date',json.loads((self.root/'state/telegram-summary.json').read_text()))
        self.run_daily(first=stuck_digest)
        self.assertEqual(len(self.sent),1)

    def test_dry_run_is_read_only_and_uses_the_identical_combined_plan(self):
        def files():return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        before=files();messages=[]
        for module in (owed_digest,stuck_digest):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(module.run(now=NOW,dry_run=True,sender=self.sender),0)
            messages.append(json.loads(output.getvalue())['telegrams'])
            self.assertEqual(files(),before)
        self.assertEqual(messages[0],messages[1]);self.assertEqual(self.sent,[])
        self.run_daily();self.assertEqual(messages[0],[self.sent[0]])

    def test_legacy_unlabelled_daily_sender_is_refused_without_transport(self):
        (self.root/'state/tg-cutover-done').unlink()
        repo=Path(owed_digest.__file__).resolve().parents[1]
        (self.root/'telegram').mkdir()
        wrapper=self.root/'telegram/tg-send.sh'
        import shlex
        wrapper.write_text('#!/bin/sh\nexec '+shlex.quote(str(repo/'scripts/tg-send.sh'))+' "$@"\n')
        wrapper.chmod(0o700)
        fakebin=self.root/'bin';fakebin.mkdir()
        curl=fakebin/'curl'
        curl.write_text('#!/bin/sh\ntouch "$DOIT_ROOT/unexpected-transport"\nprintf "200\\n"\n')
        curl.chmod(0o700)
        env=dict(os.environ,PATH=str(fakebin)+os.pathsep+os.environ['PATH'])
        with patch.dict(os.environ,env),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(owed_digest.run(now=NOW,alert=True),1)
            code='import sys;sys.path.insert(0,sys.argv[1]);import stuck_digest;from datetime import datetime;raise SystemExit(stuck_digest.run(send=True,now=datetime.fromisoformat(sys.argv[2])))'
            result=subprocess.run([sys.executable,'-c',code,str(repo/'src'),NOW.isoformat()],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,1)
        self.assertFalse((self.root/'unexpected-transport').exists())
        rows=[json.loads(l) for l in (self.root/'logs/tg-send.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r['event']=='refused' and r['label'] is None for r in rows))
        self.assertTrue(all(r['sender']=='daily-summary' for r in rows))
        self.assertNotIn('sent_date',json.loads((self.root/'state/telegram-summary.json').read_text()))


    def test_new_unmapped_engine_action_cannot_silence_the_combined_summary(self):
        from nextstep import Step
        step=Step('L-window-fixture','window','merging','action','window-default','tick',
                  'fixture:1','Elapsed window needs its normal default','default applied',
                  (NOW-timedelta(hours=1)).isoformat())
        with patch.object(stuck_digest.tick,'_steps',return_value=[step]):
            self.run_daily()
        self.assertEqual(len(self.sent),1)
        self.assertIn('1 need Thinker follow-up',self.sent[0])
        report=(self.root/'content/L-stuck-digest-2026-11-02.md').read_text()
        self.assertIn('no dwell rule',report)
        self.assertIn('window-default',report)

    def test_maintenance_severity_cannot_authorize_a_phone_interrupt(self):
        import health_alarms
        import tg_say
        def health_sender(argv):
            self.sent.append(argv[1])
            return tg_say.Delivery('sent', 200)
        import window_gate
        # A critical maintenance reading is retained, but is not observed downtime.
        alarm=dict(alarm='disk-used',severity='immediate',reading={'used_fraction':.94},line='disk pressure')
        with patch.object(health_alarms,'evaluate',return_value=[alarm]),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(health_alarms.run(now=NOW,probes={},sender=health_sender),0)
        self.assertEqual(self.sent,[])
        self.assertTrue(any(e['type']=='health-alarm' for e in fold.read_events(use_cache=False)))
        # Positive production-down and money-loss observations preserve the urgent route.
        for reading in ({'production_down':True},{'money_loss':12.5}):
            with patch.object(health_alarms,'evaluate',return_value=[dict(alarm,alarm='release-failed',reading=reading)]),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(health_alarms.run(now=NOW+timedelta(hours=len(self.sent)*25),probes={},sender=health_sender),0)
        self.assertEqual(len(self.sent),2)
        with patch.object(window_gate,'ev') as emit,patch.object(window_gate.subprocess,'run') as send:
            window_gate.record_depth(dict(configured=2,depth=1,reason='disk-over-90',disk_pct=94,red_rate=0),[],{})
        send.assert_not_called()
        self.assertEqual(emit.call_args.kwargs['telegram'],'summary')


class PaneIncidentTests(unittest.TestCase):
    def test_growing_backlog_is_one_incident_and_healthy_slow_answers_never_interrupt(self):
        with tempfile.TemporaryDirectory(prefix='pane-policy-') as tmp:
            root=Path(tmp);sent=[];items=[]
            with (patch.object(inbox,'STATE',str(root/'state.json')),patch.object(inbox,'LOG',str(root/'log')),
                 patch.object(inbox,'open_items',side_effect=lambda:items),patch.object(inbox,'pane',return_value=None),
                 patch.object(inbox,'self_pane',return_value=None),
                 patch.object(inbox.tg_say,'_transport',side_effect=lambda sender,text,root:sent.append(text) or inbox.tg_say.Delivery('sent',200)),patch.object(inbox.sys,'argv',['inbox'])):
                for i in range(6):
                    items.append(dict(src=f'q:{i}',subject='L-spec-9001',kind=None,type='question',ask='routine question',owner='thinker'))
                    inbox.main()
                self.assertEqual(len(sent),1)
                self.assertIn('unavailable',sent[0])
                with patch.object(inbox,'pane',return_value='%88'),patch.object(inbox,'pane_idle',return_value=False):
                    st=inbox.load_state();st['delivered']={r['src']:(inbox.now()-timedelta(hours=2)).isoformat() for r in items};inbox.save_state(st)
                    inbox.main();inbox.main()
                self.assertEqual(len(sent),1)
                self.assertEqual(inbox.load_state()['pane_misses'],0)
                for _ in range(3):inbox.main()
                self.assertEqual(len(sent),2)


if __name__=='__main__':
    unittest.main()
