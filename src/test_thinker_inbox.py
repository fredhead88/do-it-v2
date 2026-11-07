#!/usr/bin/env python3
"""Inbox policy regressions using synthetic ledger rows and a private root."""
import atexit
import copy
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from zoneinfo import ZoneInfo
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_IMPORT = tempfile.TemporaryDirectory(prefix='inbox-policy-import-')
atexit.register(_IMPORT.cleanup)
os.environ['HOME'] = str(Path(_IMPORT.name) / 'home')
os.environ['DOIT_ROOT'] = str(Path(_IMPORT.name) / 'state-root')
os.environ['DOIT_READ_CACHE'] = '0'
import thinker_inbox as inbox

NOW = dt.datetime(2026, 11, 7, 10, tzinfo=dt.timezone.utc)


def item(minutes=0, **fields):
    return dict(dict(owner='thinker', kind=None,
                     ts=(NOW - dt.timedelta(minutes=minutes)).isoformat()), **fields)


class PagingTests(unittest.TestCase):
    def proof(self, **facts):
        body = dict(test=self._testMethodName, assertions='passed', **facts)
        encoded = json.dumps(body, sort_keys=True).encode()
        print(json.dumps(dict(url='cli://thinker-inbox/PagingTests/'+self._testMethodName, status=200,
                              body_sha256=hashlib.sha256(encoded).hexdigest(), body_excerpt=body)))

    def test_original_age_and_precedence(self):
        cases = []
        for owner in ('thinker', 'operator'):
            for minutes in (0, 239, 240, 241):
                for kind in (None, 'merge-deadman'):
                    row = item(minutes, owner=owner, kind=kind,
                               delivered=(NOW-dt.timedelta(days=8)).isoformat())
                    expected = kind != 'merge-deadman' and (owner == 'operator' or minutes > 240)
                    with self.subTest(owner=owner, minutes=minutes, kind=kind):
                        self.assertIs(inbox.should_page(row, NOW), expected)
                    cases.append(dict(owner=owner, minutes=minutes, kind=kind, page=expected))
        # Recent pane delivery cannot reset the original event's age.
        self.assertTrue(inbox.should_page(item(241, delivered=NOW.isoformat()), NOW))
        self.proof(cases=cases, pane_delivery_does_not_reset_age=True)

    def test_unknown_age_never_ages_a_thinker_item(self):
        for value in (None, '', 'malformed', '2026-11-07T09:00:00',
                      (NOW+dt.timedelta(seconds=1)).isoformat(), 123, []):
            with self.subTest(ts=value):
                self.assertFalse(inbox.should_page(item(ts=value), NOW))
                self.assertTrue(inbox.should_page(item(ts=value, owner='operator'), NOW))
                self.assertFalse(inbox.should_page(item(ts=value, owner='operator', kind='merge-deadman'), NOW))
        self.assertFalse(inbox.should_page({'owner': 'thinker'}, NOW))
        self.assertTrue(inbox.should_page(item(240+1/60), NOW))
        # Equivalent instants with another offset use the same strict boundary.
        israel_offset = dt.timezone(dt.timedelta(hours=2))
        self.assertFalse(inbox.should_page(item(ts=(NOW-dt.timedelta(hours=4)).astimezone(israel_offset).isoformat()), NOW))
        self.proof(unknown_age_cases=7, missing_age=True, strict_seconds=True, offset_equivalence=True)

    def test_queue_counts_are_read_only_and_private(self):
        rows = [item(0, owner='operator'), item(241),
                item(300, owner='operator', kind='merge-deadman'),
                item(ts=None), item(ts='malformed', owner='operator'),
                item(-1), item(240)]
        for row in rows:
            row.update(ask='private fixture detail', src='local-ref:1', path='/private/fixture')
        before = copy.deepcopy(rows)
        expected = 'Thinker queue: 7 open; 3 operator-owned; 2 over 4 hours; 1 merge-deadman; 3 age unknown.'
        self.assertEqual(inbox.queue_line(rows, NOW), expected)
        self.assertEqual(inbox.queue_line(tuple(reversed(rows)), NOW), expected)
        self.assertEqual(rows, before)
        self.assertEqual(inbox.queue_line([], NOW),
                         'Thinker queue: 0 open; 0 operator-owned; 0 over 4 hours; 0 merge-deadman; 0 age unknown.')
        self.proof(mixed=expected, empty=inbox.queue_line([], NOW), input_unchanged=True)

    def test_real_open_items_normalizes_deadman_and_keeps_controls(self):
        rows = []
        for index, owner in enumerate(('thinker', None, 'operator')):
            row = dict(type='escalation-blocking', subject=f'deadman-{index}', kind='merge-deadman',
                       ts=(NOW-dt.timedelta(hours=5)).isoformat(),
                       why='credential quota payment', default='credential quota payment',
                       cause='credential quota payment', irreversible=True)
            if owner is not None:
                row['owner'] = owner
            rows.append(row)
        for typ in ('question', 'escalation-blocking'):
            rows.append(dict(type=typ, subject=typ, ts=NOW.isoformat(),
                             asks='credential quota payment', owner='thinker'))
        rows.append(dict(type='escalation-blocking', subject='owed', ts=NOW.isoformat(),
                         kind='owed-expired', default='credential quota payment', irreversible=True))
        rows.append(dict(type='question', subject='old',
                         ts=(NOW-dt.timedelta(days=22)).isoformat(), asks='ordinary'))
        rows.append(dict(type='question', subject='closed', ts=NOW.isoformat(), asks='ordinary'))
        rows.append(dict(type='decision', subject='other', ts=NOW.isoformat(), ref='L-executor-fixture.jsonl:8'))
        with tempfile.TemporaryDirectory(prefix='inbox-policy-ledger-') as tmp:
            root = Path(tmp)
            events = root / 'events'
            events.mkdir()
            (events / 'L-executor-fixture.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
            with patch.object(inbox.fold, 'ROOT', root), patch.object(inbox.fold, 'EVENTS', events), \
                    patch.object(inbox, 'now', return_value=NOW):
                result = inbox.open_items()
        deadman = [row for row in result if row['kind'] == 'merge-deadman']
        self.assertEqual(len(deadman), 3)
        self.assertTrue(all(row['owner'] == 'thinker' for row in deadman))
        self.assertTrue(all(not inbox.should_page(row, NOW) for row in deadman))
        self.assertEqual(inbox.queue_line(deadman, NOW),
                         'Thinker queue: 3 open; 0 operator-owned; 3 over 4 hours; 3 merge-deadman; 0 age unknown.')
        owners = {row['subject']: row['owner'] for row in result}
        self.assertEqual(owners, {'deadman-0': 'thinker', 'deadman-1': 'thinker', 'deadman-2': 'thinker',
                                  'question': 'operator', 'escalation-blocking': 'operator', 'owed': 'thinker'})
        self.proof(normalized_owners=owners, deadman_pages=0, original_retention_and_ref_answers=True)


# Every pass runs in a fresh interpreter. Ledger, inbox state, audit and holds
# are real scratch files; only tmux and outbound transport are replaced.
RUNNER = r'''
import datetime as dt, json, pathlib, sys
from unittest.mock import patch
import thinker_inbox as i
import tg_say as tg
c=json.loads(pathlib.Path(sys.argv[1]).read_text())
i.ESCALATE_FILE=str(pathlib.Path(i.ROOT)/'foreman.txt')
at=dt.datetime.fromisoformat(c['now'])
typed=[]; sent=[]
def type_into(p,text):
    typed.append([p,text])
    return c.get('type_ok',True)
def transport(sender,text,root):
    sent.append(dict(sender=sender,text=text))
    return tg.Delivery(c.get('outcome','sent'),200 if c.get('outcome','sent')=='sent' else 503)
original_fields=i.item_fields
def fields(row):
    if row['src'] in c.get('raise_refs',[]):
        raise RuntimeError('injected subsequent processing failure')
    result=original_fields(row)
    if row['src'] in c.get('invalid_refs',[]):
        result['real']=c.get('invalid_text','fixture UTC')
    return result
error=None
with patch.object(i,'now',return_value=at), patch.object(tg,'_now',return_value=at), \
     patch.object(i,'pane',return_value=c.get('pane','%normal')), \
     patch.object(i,'self_pane',return_value=c.get('self_pane')), \
     patch.object(i,'pane_idle',return_value=c.get('idle',True)), \
     patch.object(i,'type_into',side_effect=type_into), \
     patch.object(tg,'_transport',side_effect=transport), \
     patch.object(i,'item_fields',side_effect=fields):
    try:
        if c.get('bad_format'):
            with patch.object(i.notification_policy,'format_message',return_value='invalid fixture'):
                i.main()
        else:
            i.main()
        if c.get('control'):
            text=i.notification_policy.format_message('[MERGED]','Fixture completed.','Confirmed.','Read when convenient.')
            control=tg.deliver('fixture-control',text,label='[MERGED]',root=i.ROOT).state
        else: control=None
    except RuntimeError as exc:
        error=str(exc);control=None
print(json.dumps(dict(typed=typed,sent=sent,state=i.load_state(),error=error,control=control)))
'''


class ScratchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='inbox-evidence-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'events').mkdir()
        self.ledger = self.root/'events/L-executor-fixture.jsonl'
        self.config = dict(now=NOW.isoformat())
        self.trace = []
        self.env = dict(os.environ, HOME=str(self.root/'home'), DOIT_ROOT=str(self.root),
                        DOIT_READ_CACHE='0', PYTHONPATH=str(Path(inbox.__file__).resolve().parent))
        for key in ('TG_CLASS','TG_REPLY_TO','TG_LABEL','DOIT_LEDGER_FILE'):
            self.env.pop(key,None)

    def rows(self, rows):
        self.ledger.write_text(''.join(json.dumps(dict(type='question',subject='L-spec-9001',
            asks='ordinary fixture',ts=NOW.isoformat()) | row)+'\n' for row in rows))
        return {f'L-executor-fixture.jsonl:{n}' for n in range(1,len(rows)+1)}

    def run_pass(self, **config):
        self.config.update(config)
        (self.root/'fixture.json').write_text(json.dumps(self.config))
        result=subprocess.run([sys.executable,'-c',RUNNER,str(self.root/'fixture.json')],
                              env=self.env,cwd=self.root,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        observed=json.loads(result.stdout)
        self.trace.append(dict(sent=observed['sent'],typed=observed['typed'],error=observed['error']))
        return observed

    def read_state(self):
        result=subprocess.run([sys.executable,'-c','import json,thinker_inbox as i; print(json.dumps(i.load_state()))'],
                              env=self.env,cwd=self.root,text=True,capture_output=True,check=True)
        return json.loads(result.stdout)

    def proof(self, **facts):
        body=dict(test=self._testMethodName,assertions='passed',trace=self.trace,**facts)
        encoded=json.dumps(body,sort_keys=True).encode()
        print(json.dumps(dict(url='scratch://thinker-inbox/'+type(self).__name__+'/'+self._testMethodName,
            status=200,body_sha256=hashlib.sha256(encoded).hexdigest(),body_excerpt=body)))

    def log_rows(self):
        p=self.root/'logs/thinker-inbox.log'
        return p.read_text().splitlines() if p.exists() else []


class InboxDeliveryTests(ScratchCase):
    def test_routing_foreman_and_failed_typing(self):
        self.rows([{},dict(subject='L-charter-9001'),dict(asks='Approve payment')])
        planner=self.root/'events/L-planner-fixture.jsonl'
        planner.write_text(json.dumps(dict(type='question',subject='L-spec-9002',asks='ordinary',ts=NOW.isoformat()))+'\n')
        foreman=self.root/'foreman.txt';foreman.write_text('baseline\n')
        r=self.run_pass(self_pane='%self',type_ok=False)
        self.assertEqual(r['state']['delivered'],{})
        self.assertEqual(r['state']['esc_lines'],1)
        foreman.write_text('baseline\n\nnew literal $(false); `false`\n')
        r=self.run_pass(type_ok=True)
        self.assertEqual(len(r['state']['delivered']),4)
        self.assertEqual(r['state']['esc_lines'],3)
        direct=[text for pane,text in r['typed'] if pane=='%self' and 'DIRECT' in text]
        self.assertEqual(len(direct),1)
        for expected in ('L-planner-fixture','L-charter-9001','FOR THE OPERATOR'):
            self.assertIn(expected,direct[0])
        self.assertTrue(any('$(false); `false`' in text for _,text in r['typed']))
        self.assertEqual(self.run_pass()['typed'],[])
        self.rows([]);planner.write_text('')
        r=self.run_pass();self.assertEqual(r['state']['delivered'],{})
        self.assertEqual(r['state']['tg'],{})
        self.proof(routes=['normal','planner','charter','operator','foreman'],failed_typing_retry=True,closed_pruned=True)

    def test_outage_retry_recovery_and_deadman(self):
        refs=self.rows([dict(ts=(NOW-dt.timedelta(hours=5)).isoformat()),
                        dict(type='escalation-blocking',kind='merge-deadman',asks='payment',irreversible=True)])
        for _ in range(2):self.assertEqual(self.run_pass(pane=None,self_pane='%direct')['sent'],[])
        r=self.run_pass(outcome='failed');self.assertEqual(len(r['sent']),1)
        self.assertEqual(r['state']['tg'],{})
        r=self.run_pass(outcome='sent');self.assertEqual(len(r['sent']),1)
        self.assertTrue(r['sent'][0]['text'].startswith('[DOWN]'))
        self.assertEqual(r['state']['tg'],{})
        self.assertEqual(self.run_pass()['sent'],[])
        r=self.run_pass(pane='%normal');self.assertEqual(len(r['sent']),1)
        self.assertEqual(set(r['state']['tg']),{sorted(refs)[0]})
        self.assertEqual(self.run_pass()['sent'],[])
        for _ in range(2):self.assertEqual(self.run_pass(pane=None)['sent'],[])
        self.assertEqual(len(self.run_pass()['sent']),1)
        self.proof(outage_threshold=3,failed_retried=True,recovery_rearmed=True,no_deadman_item_page=True)

    def test_real_refusal_is_terminal_per_ref_and_new_ref_is_eligible(self):
        refs=self.rows([dict(asks='payment'),dict(asks='quota')])
        r=self.run_pass(bad_format=True)
        self.assertEqual(r['sent'],[]);self.assertEqual(r['state']['tg'],{})
        self.assertEqual(set(r['state']['tg_refused']),refs)
        audit=self.root/'logs/tg-send.jsonl'
        self.assertEqual(len(audit.read_text().splitlines()),1)
        self.assertEqual(len(self.log_rows()),3) # normal pane success plus two private refusals
        self.run_pass();self.run_pass()
        self.assertEqual(len(audit.read_text().splitlines()),1)
        refused=[line for line in self.log_rows() if 'refused' in line]
        self.assertEqual(len(refused),2)
        self.rows([dict(asks='payment'),dict(asks='quota'),dict(asks='payment')])
        r=self.run_pass(bad_format=False)
        self.assertEqual(len(r['sent']),1)
        self.assertEqual(set(r['state']['tg']),{'L-executor-fixture.jsonl:3'})
        self.proof(refused_refs=sorted(refs),private_refusals=refused,batch_audit_rows=1,new_ref_sent=True)

    def test_formatter_value_error_is_safe_isolated_and_durable(self):
        refs=sorted(self.rows([dict(asks='payment') for _ in range(3)]))
        r=self.run_pass(invalid_refs=refs[:1],invalid_text='fixture UTC')
        self.assertIsNone(r['error'])
        self.assertIn(refs[0],r['state']['tg_refused'])
        self.assertEqual(set(r['state']['tg']),set(refs[1:]))
        # Use independent source refs for both invalid fields in a single pass.
        self.rows([{}, {}, {}, dict(asks='payment'),dict(asks='payment')])
        r=self.run_pass(invalid_refs=['L-executor-fixture.jsonl:4'],invalid_text='/private/fixture')
        self.assertIn('L-executor-fixture.jsonl:4',r['state']['tg_refused'])
        self.assertIn('L-executor-fixture.jsonl:5',r['state']['tg'])
        before=self.log_rows();self.run_pass();self.assertEqual(self.log_rows(),before)
        text='\n'.join(self.log_rows())
        self.assertNotIn('/private/fixture',text);self.assertNotIn('fixture UTC',text)
        self.proof(terminal_refs=sorted(self.read_state()['tg_refused']),safe_log=True,other_work_sent=True)

    def test_real_overnight_clocks_and_held_control(self):
        for hour,minute in ((23,30),(6,30)):
            at=NOW.astimezone(ZoneInfo('Asia/Jerusalem')).replace(hour=hour,minute=minute)
            self.rows([dict(asks='payment',subject='L-spec-9001')])
            (self.root/'state/thinker-inbox.json').unlink(missing_ok=True)
            r=self.run_pass(now=at.isoformat())
            self.assertEqual(len(r['sent']),1);self.assertTrue(r['sent'][0]['text'].startswith('[YOU]'))
            self.assertEqual(r['sent'][0]['sender'],'thinker-inbox')
            self.assertFalse((self.root/'state/tg-held.jsonl').exists())
            self.assertEqual(self.run_pass()['sent'],[])
            for _ in range(2):self.assertEqual(self.run_pass(pane=None)['sent'],[])
            r=self.run_pass();self.assertEqual(len(r['sent']),1)
            self.assertTrue(r['sent'][0]['text'].startswith('[DOWN]'))
            self.assertFalse((self.root/'state/tg-held.jsonl').exists())
            self.assertEqual(self.run_pass()['sent'],[])
            r=self.run_pass(control=True);self.assertEqual(r['control'],'held');self.assertEqual(r['sent'],[])
            self.assertEqual(len((self.root/'state/tg-held.jsonl').read_text().splitlines()),1)
            (self.root/'state/tg-held.jsonl').unlink()
            self.config.pop('control',None);self.config.pop('pane',None)
        self.proof(clocks=['23:30 Asia/Jerusalem','06:30 Asia/Jerusalem'],you_down_sent=True,merged_held=True)

    def test_anomalous_held_and_refused_outage_do_not_retry(self):
        self.rows([dict(asks='payment')])
        r=self.run_pass(outcome='held')
        self.assertEqual(r['state']['tg'],{});self.assertEqual(len(r['state']['tg_pending']),1)
        self.assertEqual(self.run_pass()['sent'],[])
        self.rows([]);r=self.run_pass();self.assertEqual(r['state']['tg_pending'],{})
        for _ in range(2):self.run_pass(pane=None)
        r=self.run_pass();self.assertTrue(r['state']['pane_pending'])
        self.assertFalse(r['state']['pane_alerted']);self.assertEqual(self.run_pass()['sent'],[])
        r=self.run_pass(pane='%normal');self.assertFalse(r['state']['pane_pending'])
        for _ in range(3):r=self.run_pass(pane=None,bad_format=True)
        self.assertTrue(r['state']['pane_refused']);before=self.log_rows();self.run_pass()
        self.assertEqual(before,self.log_rows())
        self.proof(held_pending=True,closure_pruned=True,outage_refusal_terminal=True,recovery_clears=True)


class StateRoundTripTests(ScratchCase):
    def assert_survives(self, route):
        old=(NOW-dt.timedelta(hours=5)).isoformat()
        refs=sorted(self.rows([dict(ts=old,subject='L-charter-9001' if route=='direct' else 'L-spec-9001'),dict(ts=old)]))
        config=dict(self_pane='%direct' if route in ('direct','foreman') else None)
        if route=='foreman':
            foreman=self.root/'foreman.txt';foreman.write_text('baseline\n')
            self.run_pass(**config,type_ok=False,idle=False,outcome='failed')
            foreman.write_text('baseline\nnew line\n')
        r=self.run_pass(**config,type_ok=True,idle=True,raise_refs=[refs[1]])
        self.assertIsNotNone(r['error'])
        state=self.read_state();self.assertEqual(set(state['delivered']),set(refs))
        if route=='foreman':self.assertEqual(state['esc_lines'],2)
        if route=='direct':self.assertTrue(any(p=='%direct' for p,text in r['typed']))
        self.assertEqual(state['tg'],{})
        r=self.run_pass(raise_refs=[],outcome='sent');self.assertEqual(r['typed'],[]);self.assertEqual(len(r['sent']),1)
        self.assertEqual(self.run_pass()['sent'],[])
        self.proof(route=route,fresh_process_state=state,typed_actions_not_repeated=True,failing_action_retried=True)

    def test_normal_success_survives_later_exception(self):
        self.assert_survives('normal')

    def test_direct_success_survives_later_exception(self):
        self.assert_survives('direct')

    def test_foreman_success_survives_later_exception(self):
        self.assert_survives('foreman')

    def test_failed_foreman_and_normal_typing_retry_after_restart(self):
        self.rows([{}]);(self.root/'foreman.txt').write_text('baseline\n')
        self.run_pass(self_pane='%direct',type_ok=False)
        (self.root/'foreman.txt').write_text('baseline\nappended\n')
        r=self.run_pass();self.assertEqual(r['state']['esc_lines'],1);self.assertEqual(r['state']['delivered'],{})
        r=self.run_pass(type_ok=True);self.assertEqual(r['state']['esc_lines'],2)
        self.assertEqual(len(r['state']['delivered']),1);self.assertEqual(self.run_pass()['typed'],[])
        self.proof(failed_foreman_cursor_preserved=True,normal_and_foreman_retried=True)

    def test_legacy_state_success_and_failed_roundtrips(self):
        refs=self.rows([dict(asks='payment')]);(self.root/'state').mkdir()
        (self.root/'state/thinker-inbox.json').write_text(json.dumps(dict(delivered={},tg={},legacy='retained')))
        r=self.run_pass(outcome='failed');self.assertEqual(self.read_state()['tg'],{})
        r=self.run_pass(outcome='sent');self.assertEqual(set(self.read_state()['tg']),refs)
        self.assertEqual(self.read_state()['legacy'],'retained');self.assertEqual(self.run_pass()['sent'],[])
        self.proof(legacy_preserved=True,failed_retry=True,committed_refs=sorted(refs))


class VersionedEntryTests(ScratchCase):
    def test_other_cwd_symlink_list_and_root_are_read_only(self):
        self.rows([{}]);link=self.root/'thinker-inbox.py';link.symlink_to(Path(inbox.__file__).resolve())
        def snapshot():return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        before=snapshot()
        for path in (str(Path(inbox.__file__).resolve()),str(link)):
            r=subprocess.run([sys.executable,path,'--list'],cwd=self.root,env={k:v for k,v in self.env.items() if k!='PYTHONPATH'},text=True,capture_output=True)
            self.assertEqual(r.returncode,0,r.stderr);self.assertIn('1 open',r.stdout)
        self.assertEqual(before,snapshot())
        r=self.run_pass();self.assertEqual(len(r['state']['delivered']),1)
        self.assertTrue((self.root/'state/thinker-inbox.json').is_file())
        self.proof(other_cwd=True,symlink=True,list_read_only=True,preimport_root=True)

    def test_manifest_signature_and_fixture_migration(self):
        import crons
        manifest=Path(inbox.__file__).resolve().parents[1]/'crons.toml'
        rows=crons.rows(manifest);matched=[r for r in rows if r['name']=='thinker-inbox']
        self.assertEqual(len(matched),1);row=matched[0]
        self.assertEqual(row['schedule'],'*/5 * * * *');self.assertEqual(row['owner'],'thinker')
        self.assertEqual(row['project'],'do-it-v2');self.assertIn('cron-run thinker-inbox --',row['command'])
        self.assertIn('/src/thinker_inbox.py',row['command'])
        legacy='/usr/bin/python3 /home/albert/.do-it/thinker-inbox.py'
        good=[legacy,'*/5 * * * * '+legacy,row['command'],crons.print_line('thinker-inbox')]
        for text in good:self.assertIsNotNone(crons._find_line(text,row['sig']))
        for text in ('# '+legacy,'/usr/bin/python3 /fixture/thinker-inbox.py-helper'):
            self.assertIsNone(crons._find_line(text,row['sig']))
        target=self.root/'crontab';other='# keep comment\n7 * * * * /fixture/other\n'
        target.write_text(other+'*/5 * * * * '+legacy+'\n'+good[-1]+'\n')
        with patch.object(crons,'_RUN',side_effect=AssertionError('external boundary')):
            crons.install('thinker-inbox',manifest=manifest,crontab_path=target)
            first=target.read_text();crons.install('thinker-inbox',manifest=manifest,crontab_path=target)
        self.assertEqual(first,target.read_text());self.assertTrue(first.startswith(other))
        self.assertEqual(first.count('# doit-cron:thinker-inbox'),1)
        self.proof(manifest_row=row,signature_cases=good,fixture_after=first)


class BacklogBatchTests(ScratchCase):
    def backlog(self,eligible=84,lane=0):
        rows=[dict(ts=(NOW-dt.timedelta(hours=5)).isoformat(),subject='x'*10000,
                   asks='private fixture $(false); `false`') for _ in range(eligible)]
        rows += [dict(type='escalation-blocking',subject=f'L-spec-{9100+n}',kind='lane-stale',ts=(NOW-dt.timedelta(hours=5)).isoformat()) for n in range(lane)]
        rows += [{} for _ in range(25)]
        self.rows(rows)
        return {f'L-executor-fixture.jsonl:{n}' for n in range(1,eligible+1)}

    def assert_batch(self,r,expected):
        self.assertEqual(len(r['sent']),1)
        text=r['sent'][0]['text'];self.assertIn(str(len(expected))+' eligible',text)
        self.assertIn(str(max(0,len(expected)-3))+' omitted',text)
        self.assertLessEqual(len(text),3500);self.assertNotIn('private fixture',text)
        self.assertNotIn('$(false)',text);self.assertNotIn('x'*100,text)
        self.assertEqual(set(self.read_state()['tg']),expected)

    def test_full_backlog_first_activation_and_recovery(self):
        expected=self.backlog()
        (self.root/'state').mkdir();state=self.root/'state/thinker-inbox.json'
        state.write_text(json.dumps(dict(delivered={},tg={})))
        r=self.run_pass();self.assert_batch(r,expected)
        self.assertEqual(self.run_pass()['sent'],[])
        self.proof(expected_refs=sorted(expected),committed_refs=sorted(self.read_state()['tg']),eligible=84,omitted=81)
        state.write_text(json.dumps(dict(delivered={},tg={})))
        for _ in range(3):r=self.run_pass(pane=None)
        self.assertEqual(len(r['sent']),1);self.assertEqual(self.read_state()['tg'],{})
        r=self.run_pass(pane='%normal');self.assert_batch(r,expected)
        self.assertEqual(self.run_pass()['sent'],[])

    def test_failed_batch_marks_none_then_retries_and_single_zero(self):
        expected=self.backlog();r=self.run_pass(outcome='failed')
        self.assertEqual(len(r['sent']),1);self.assertEqual(self.read_state()['tg'],{})
        r=self.run_pass(outcome='sent');self.assert_batch(r,expected)
        self.rows([]);self.run_pass() # close the prior source set before a single new item
        self.rows([dict(asks='payment')]);r=self.run_pass()
        self.assertEqual(len(r['sent']),1);self.assertIn('1 eligible',r['sent'][0]['text'])
        self.rows([]);self.assertEqual(self.run_pass()['sent'],[])
        self.proof(failed_batch_unmarked=True,single_sent=True,empty_silent=True)


class LaneStaleTests(ScratchCase):
    backlog = BacklogBatchTests.backlog
    assert_batch = BacklogBatchTests.assert_batch

    def test_policy_matrix(self):
        cases=[]
        for representation in ({'kind':'lane-stale'},{'subject':'lane-stale:fixture'}):
            for owner in ('operator','thinker'):
                for age in (0,240,241):
                    for up in (True,False,None):
                        row=item(age,owner=owner,**representation)
                        if up is not None:row['pane_up']=up
                        before=copy.deepcopy(row)
                        expected=up is False and (owner=='operator' or age>240)
                        self.assertIs(inbox.should_page(row,NOW),expected);self.assertEqual(row,before)
                        self.assertFalse(inbox.should_page(dict(row,kind='merge-deadman'),NOW))
                        cases.append(dict(row=row,page=expected))
        self.proof(cases=cases)

    def test_mixed_backlog_busy_absent_and_recovery(self):
        expected=self.backlog(eligible=32,lane=52)
        r=self.run_pass(self_pane='%direct');self.assert_batch(r,expected)
        self.assertEqual(len(r['state']['delivered']),109)
        state=self.root/'state/thinker-inbox.json';state.unlink()
        r=self.run_pass(idle=False);self.assert_batch(r,expected)
        self.assertEqual(r['typed'],[])
        state.unlink()
        for _ in range(3):r=self.run_pass(pane=None)
        self.assertEqual(r['state']['tg'],{});self.assertTrue(r['sent'][0]['text'].startswith('[DOWN]'))
        r=self.run_pass(pane='%normal',idle=True);self.assert_batch(r,expected)
        self.assertEqual(len(r['state']['delivered']),109)
        with patch.object(inbox.fold,'ROOT',self.root),patch.object(inbox.fold,'EVENTS',self.root/'events'),patch.object(inbox,'now',return_value=NOW):
            line=inbox.queue_line(inbox.open_items(),NOW)
        self.assertIn('109 open',line);self.assertIn('84 over 4 hours',line)
        self.proof(expected_refs=sorted(expected),committed_refs=sorted(self.read_state()['tg']),lane_stale=52,queue=line,busy_is_up=True)


class TestMasterFullInbox(unittest.TestCase):
    def test_full_question_rendering_is_inert_and_untruncated(self):
        ask = "failed: tests/test_full.py::test_red; " + "merge subject " * 25 + "END\n\x1b[31m\u2028command"
        item = dict(subject="master-full:albert-scott:1", kind="master-full-red", type="question",
                    src="L-base-red-local.jsonl:1", ask=ask)
        rendered = inbox.short(item)
        self.assertIn("END", rendered)
        self.assertNotIn("\n", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u2028", rendered)
        self.assertIn("tests/test_full.py::test_red", rendered)

    def test_literal_send_and_answer_ref_preserved(self):
        with patch.object(inbox.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                patch.object(inbox.time, "sleep"):
            self.assertTrue(inbox.type_into("%1", "$(untrusted) ; subject"))
        self.assertEqual(run.call_args_list[0].args[0],
                         ["tmux", "send-keys", "-t", "%1", "-l", "$(untrusted) ; subject"])
        self.assertEqual(run.call_args_list[1].args[0][-1], "Enter")

    def test_list_contains_complete_attribution(self):
        item = dict(owner="thinker", subject="master-full:albert-scott:1", kind="master-full-red",
                    type="question", src="L-base-red-local.jsonl:1", ask="x" * 200 + " merge: last-tested-head")
        import contextlib
        import io
        output = io.StringIO()
        with patch.object(inbox, "open_items", return_value=[item]), \
                patch.object(sys, "argv", ["thinker_inbox.py", "--list"]), \
                contextlib.redirect_stdout(output):
            inbox.main()
        self.assertIn("merge: last-tested-head", output.getvalue())
        self.assertIn("1 open", output.getvalue())

    def test_delivery_keeps_all_master_full_details(self):
        items = [dict(owner="thinker", subject=f"master-full:albert-scott:{n}", kind="master-full-red",
                      type="question", src=f"L-base-red-local.jsonl:{n}",
                      ask="detail " * 180 + f" final-merge-{n}") for n in range(4)]
        with patch.object(inbox, "open_items", return_value=items), \
                patch.object(inbox, "load_state", return_value={"delivered": {}, "tg": {}}), \
                patch.object(inbox, "pane", return_value="%1"), patch.object(inbox, "pane_idle", return_value=True), \
                patch.object(inbox, "type_into", return_value=True) as send, \
                patch.object(inbox, "save_state"), patch.object(inbox, "log"), \
                patch.object(sys, "argv", ["thinker_inbox.py"]):
            inbox.main()
        message = send.call_args.args[1]
        for n in range(4): self.assertIn(f"final-merge-{n}", message)


if __name__ == '__main__':
    unittest.main()
