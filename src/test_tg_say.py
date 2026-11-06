"""Scratch-only transport and restart tests; no network or account credentials."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import tg_say as tg
from notification_policy import format_message, validate_message

REPO = Path(__file__).resolve().parents[1]
TEXT = format_message('[YOU]', 'Ready', 'Confirmed https://example.org/result', 'Review', None, None)
# Fixture secrets are generated per test, consumed privately, never printed in captures.
CURL = '''#!/usr/bin/python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ['DOIT_ROOT'])
args = sys.argv[1:]
config = sys.stdin.read()
secret = (root/'telegram.token').read_text().strip()
assert secret not in ' '.join(args)
assert args[args.index('-K')+1] == '-'
assert secret in config
assert 'url = "https://api.telegram.org/bot' in config
with (root/'capture').open('a') as out:
    out.write(json.dumps(args)+'\\n')
if os.environ.get('BREAK_LOG'):
    (root/'logs/tg-send.jsonl').unlink()
    (root/'logs/tg-send.jsonl').symlink_to(root/'not-created')
if os.environ.get('ENQUEUE'):
    sys.path.insert(0, os.environ['SOURCE'])
    import tg_say as tg
    row = json.loads(os.environ['ENQUEUE'])
    with tg._private(root/'state/tg-held.lock'):
        tg._append(root/'state/tg-held.jsonl',row)
code = os.environ.get('HTTP','200')
# Fail just the merged group if requested.
if os.environ.get('FAIL_MERGED') and any('[MERGED]' in a for a in args): code='500'
print(code)
sys.exit(7 if code == '000' else 0)
'''


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tg-core-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'telegram').mkdir()
        (self.root/'telegram.token').write_text(os.urandom(16).hex())
        (self.root/'telegram/chat_id').write_text('12345')
        (self.root/'bin').mkdir()
        fake = self.root/'bin/curl'; fake.write_text(CURL); fake.chmod(0o700)
        self.env = dict(os.environ, DOIT_ROOT=str(self.root), TG_SENDER='fixture',
                        PATH=str(self.root/'bin')+':/usr/bin:/bin', SOURCE=str(REPO/'src'))
        for name in ('TG_CLASS', 'TG_REPLY_TO', 'TG_LABEL', 'HTTP', 'BREAK_LOG', 'ENQUEUE', 'FAIL_MERGED'):
            self.env.pop(name, None)
        self.env_patch = patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)

    def run_sender(self, *args, env=None, stdin=None):
        return subprocess.run([str(REPO/'scripts/tg-send.sh'), *args], env=self.env | (env or {}),
                              input=stdin, text=True, capture_output=True, timeout=10)

    def rows(self):
        # Read in a different process, so buffers/same-object state cannot satisfy persistence.
        code = 'import json,pathlib,sys;print(json.dumps([json.loads(l) for l in pathlib.Path(sys.argv[1]).read_text().splitlines()]))'
        return json.loads(subprocess.check_output([sys.executable, '-c', code, str(self.root/'logs/tg-send.jsonl')], text=True))

    def calls(self):
        path = self.root/'capture'
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def drain(self, **env):
        code = 'import sys,json;from dataclasses import asdict;from datetime import datetime;sys.path.insert(0,sys.argv[1]);import tg_say as t;print(json.dumps([asdict(r) for r in t.drain_held(datetime.fromisoformat(sys.argv[2]))]))'
        result = subprocess.run([sys.executable, '-c', code, str(REPO/'src'), '2026-01-15T07:00:00+02:00'],
                                env=self.env | env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def enqueue(self, label, text=None, sender='fixture'):
        text = text or format_message(label, 'Ready', 'Confirmed', 'Read', None, None)
        with patch.object(tg, '_now', return_value=datetime.fromisoformat('2026-01-15T23:00:00+02:00')):
            self.assertEqual(tg.deliver(sender, text, label=label, root=self.root), tg.Delivery('held'))
        return text


class TransportTests(Fixture):
    def test_response_matrix_and_private_paired_audit(self):
        for code in ('200', '400', '429', '500', '000'):
            result = self.run_sender(TEXT, env={'HTTP': code})
            self.assertEqual(result.stdout.strip(), code)
            self.assertEqual(result.returncode == 0, code == '200')
        rows = self.rows()
        self.assertEqual(len(rows), 10)
        self.assertEqual(len({r['id'] for r in rows}), 5)
        for attempt, result in zip(rows[::2], rows[1::2]):
            self.assertEqual(attempt['event'], 'attempt'); self.assertEqual(result['event'], 'result')
            self.assertEqual(attempt['id'], result['id']); self.assertIsNone(attempt['http'])
            self.assertEqual(attempt['length'], len(TEXT)); self.assertEqual(attempt['label'], '[YOU]')
            self.assertRegex(attempt['time'], r'\+0[23]:00$')
        self.assertIsNone(rows[-1]['http'])
        self.assertEqual((self.root/'logs/tg-send.jsonl').stat().st_mode & 0o777, 0o600)
        audit = json.dumps(rows)
        self.assertNotIn(TEXT, audit); self.assertNotIn('12345', audit)
        self.assertNotIn((self.root/'telegram.token').read_text(), audit)

    def test_refusal_credentials_and_pre_log_failure(self):
        for env, text in (({'TG_SENDER':''}, TEXT), ({'TG_SENDER':'   '},TEXT), ({}, 'Unlabelled')):
            result = self.run_sender(text, env=env)
            self.assertEqual(result.returncode, 2); self.assertEqual(result.stdout.strip(), '000')
        self.assertEqual(self.calls(), [])
        (self.root/'telegram.token').unlink()
        self.assertNotEqual(self.run_sender(TEXT).returncode,0)
        self.assertEqual(self.calls(), [])
        log = self.root/'logs/tg-send.jsonl'; log.unlink(); log.symlink_to(self.root/'absent')
        self.assertNotEqual(self.run_sender(TEXT).returncode,0)
        self.assertFalse((self.root/'absent').exists()); self.assertEqual(self.calls(), [])

    def test_result_log_failure_is_failed_even_after_200(self):
        result = self.run_sender(TEXT, env={'BREAK_LOG':'1'})
        self.assertNotEqual(result.returncode,0); self.assertEqual(result.stdout.strip(),'200')
        self.assertEqual(len(self.calls()),1); self.assertFalse((self.root/'not-created').exists())


class DocumentReplyTests(Fixture):
    def test_documents_prefix_channel_and_data(self):
        path=self.root/'quotes";$(touch bad)\nfile'; path.write_text('document')
        text=TEXT+'\nQuotes " and $(touch bad) stay data'
        for executable, args in ((REPO/'scripts/tg-send.sh', ['--document',str(path),text]),
                                 (REPO/'scripts/tg-send-file.sh',[str(path),text])):
            result=subprocess.run([str(executable),*args],env=self.env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse((self.root/'bad').exists())
        for args in self.calls():
            self.assertIn('caption='+text,args)
        for invalid in ('bad',' [YOU] event','\n'+TEXT,'[UNKNOWN] event'):
            self.assertNotEqual(self.run_sender('--document',str(path),invalid,env={'TG_LABEL':'[YOU]'}).returncode,0)
        daily=format_message('[DAILY]','Ready','Checked','Read')
        self.assertEqual(self.run_sender('--document',str(path),daily,env={'TG_LABEL':'[YOU]'}).returncode,0)
        self.assertEqual(self.rows()[-1]['label'],'[DAILY]')
        self.assertNotEqual(self.run_sender('--document',str(self.root/'missing'),TEXT).returncode,0)
        self.assertNotEqual(self.run_sender('--document','',TEXT).returncode,0)
        self.assertEqual(self.run_sender('--document').stdout.strip(),'000')
        self.assertEqual(self.rows()[-1]['event'],'refused')

    def test_omitted_caption_does_not_read_open_stdin_and_text_stdin_works(self):
        path=self.root/'file';path.write_text('fixture')
        for executable,args in ((REPO/'scripts/tg-send.sh',['--document',str(path)]),
                                (REPO/'scripts/tg-send-file.sh',[str(path)])):
            proc=subprocess.Popen([str(executable),*args],env=self.env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.assertEqual(proc.wait(timeout=5),2)
                self.assertEqual(proc.stdout.read().strip(),'000')
            finally:
                if proc.poll() is None: proc.kill();proc.wait()
                proc.stdin.close();proc.stdout.close();proc.stderr.close()
        self.assertEqual(self.calls(),[])
        self.assertEqual(self.run_sender(stdin=TEXT).returncode,0)

    def test_reply_exception_requires_class_and_positive_inbound_id(self):
        for reply_id in ('', '0', '-1', 'abc', '1; exit'):
            self.assertNotEqual(self.run_sender('Received.',env={'TG_CLASS':'reply','TG_REPLY_TO':reply_id}).returncode,0)
        self.assertNotEqual(self.run_sender('Received.',env={'TG_REPLY_TO':'42'}).returncode,0)
        self.assertEqual(self.calls(),[])
        self.assertEqual(self.run_sender('Received.',env={'TG_CLASS':'reply','TG_REPLY_TO':'42'}).returncode,0)
        row=self.rows()[-1]; self.assertEqual(row['class'],'reply');self.assertEqual(row['reply_to'],'42');self.assertIsNone(row['label'])
        path=self.root/'file';path.write_text('doc')
        self.assertEqual(self.run_sender('--document',str(path),env={'TG_CLASS':'reply','TG_REPLY_TO':'42'}).returncode,0)


class DeliveryTests(Fixture):
    def test_truthful_matrix_and_environment(self):
        for code in ('200','400','429','500','000'):
            with patch.dict(os.environ,HTTP=code,TG_CLASS='reply',TG_REPLY_TO='42',TG_LABEL='[DOWN]'):
                result=tg.deliver('named',TEXT,label='[YOU]',root=self.root)
            self.assertEqual(result,tg.Delivery('sent' if code=='200' else 'failed',int(code) or None))
        self.assertTrue(all(r['class']=='automated' and r['sender']=='named' and r['label']=='[YOU]' for r in self.rows()))
        for label,text in (('[DAILY]',TEXT),(None,TEXT),('[YOU]','[YOU] bad'),('[YOU]',TEXT+' UTC')):
            self.assertEqual(tg.deliver('named',text,label=label,root=self.root),tg.Delivery('refused'))
        self.assertEqual(len(self.calls()),5)
        refused=self.rows()[-4:]
        self.assertTrue(all(r['event']=='refused' and r['class']=='automated' for r in refused))
        with patch.object(tg.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'429\n','')):
            self.assertEqual(tg.deliver('named',TEXT,label='[YOU]',root=self.root),tg.Delivery('failed',429))
        with patch.dict(os.environ,BREAK_LOG='1'):
            self.assertEqual(tg.deliver('named',TEXT,label='[YOU]',root=self.root),tg.Delivery('failed',200))


class QuietTests(Fixture):
    def test_boundaries_and_dst(self):
        for date,offset in (('2026-01-15','+02:00'),('2026-07-15','+03:00')):
            for clock,expected in (('22:59:59',False),('23:00:00',True),('06:59:59',True),('07:00:00',False)):
                now=datetime.fromisoformat(date+'T'+clock+offset)
                self.assertEqual(tg.quiet(now.astimezone(timezone.utc)),expected)
                for label in ('[YOU]','[DOWN]','[MERGED]','[DAILY]'):
                    text=format_message(label,'Ready','Confirmed','Read')
                    with patch.object(tg,'_now',return_value=now):
                        result=tg.deliver('fixture',text,label=label,root=self.root)
                    self.assertEqual(result.state,'held' if expected and label in ('[MERGED]','[DAILY]') else 'sent')
        for stamp,hour in (('2026-03-26T23:59:59+00:00',1),('2026-03-27T00:00:00+00:00',3),
                           ('2026-10-24T22:59:59+00:00',1),('2026-10-24T23:00:00+00:00',1)):
            now=datetime.fromisoformat(stamp)
            self.assertEqual(now.astimezone(ZoneInfo('Asia/Jerusalem')).hour,hour)
            self.assertTrue(tg.quiet(now))
        with self.assertRaises(ValueError):tg.quiet(datetime(2026,1,15))
        before=(self.root/'state/tg-held.jsonl').read_bytes();calls=len(self.calls())
        self.assertEqual(tg.drain_held(datetime.fromisoformat('2026-01-15T23:00:00+02:00'),root=self.root),[])
        self.assertEqual((self.root/'state/tg-held.jsonl').read_bytes(),before);self.assertEqual(len(self.calls()),calls)


class StoreRoundTripTests(Fixture):
    def test_fresh_drain_exact_group_and_ack(self):
        originals=[self.enqueue('[MERGED]',format_message('[MERGED]',f'Ready {i}','Confirmed',f'Action {i}'),sender=f'sender-{i}') for i in range(2)]
        daily=self.enqueue('[DAILY]')
        self.assertEqual(self.calls(),[])
        self.assertEqual(self.drain(),[{'state':'sent','http':200}]*2)
        texts=[next(a[5:] for a in args if a.startswith('text=')) for args in self.calls()]
        self.assertEqual(texts[0],format_message('[MERGED]','Held notifications: 2','\n\n'.join(originals),'Read the held notices below for any action.'))
        self.assertIn(daily,texts[1]);self.assertEqual(self.drain(),[])
        self.assertTrue(all(r['sender']=='drain-held' for r in self.rows()))

    def test_bounded_whole_rows_and_exact_omissions(self):
        for huge_first in (False,True):
            originals=[]
            for i in range(12):
                text=format_message('[MERGED]',f'Item {i}', '\U0001f680'*(2500 if huge_first and i==0 else 240),'Read')
                originals.append(self.enqueue('[MERGED]',text))
            self.assertEqual(self.drain(),[{'state':'sent','http':200}])
            text=next(a[5:] for a in self.calls()[-1] if a.startswith('text='))
            validate_message(text,'[MERGED]');self.assertLessEqual(len(text.encode('utf-16-le'))//2,4096)
            kept=sum(original in text for original in originals)
            self.assertIn(f'and {12-kept} more held notifications omitted',text)
            expected=tg._group('[MERGED]',[{'text':x} for x in originals])
            self.assertEqual(text,expected)
            if huge_first:self.assertEqual(kept,0)
            else:
                more=format_message('[MERGED]','Held notifications: 12','\n\n'.join(originals[:kept+1]+[f'and {11-kept} more held notifications omitted']),'Read the held notices below for any action.')
                self.assertGreater(len(more.encode('utf-16-le'))//2,4096)
            self.assertEqual(self.drain(),[])

    def test_failed_group_concurrent_enqueue_and_corruption(self):
        self.enqueue('[MERGED]');self.enqueue('[DAILY]')
        before=(self.root/'state/tg-held.jsonl').read_text().splitlines()[0]
        self.assertEqual(self.drain(FAIL_MERGED='1'),[{'state':'failed','http':500},{'state':'sent','http':200}])
        self.assertEqual((self.root/'state/tg-held.jsonl').read_text().strip(),before)
        row=json.loads(before);row['id']='concurrent';row['text']=format_message('[MERGED]','New arrival','Confirmed','Read')
        self.assertEqual(self.drain(ENQUEUE=json.dumps(row)),[{'state':'sent','http':200}])
        pending=json.loads((self.root/'state/tg-held.jsonl').read_text());self.assertEqual(pending['id'],'concurrent')
        self.assertEqual(self.drain(),[{'state':'sent','http':200}]);self.assertEqual(self.drain(),[])
        path=self.root/'state/tg-held.jsonl';path.write_text('{broken\n');before=path.read_bytes()
        self.assertEqual(self.drain(),[{'state':'failed','http':None}]);self.assertEqual(path.read_bytes(),before)


class CliTests(Fixture):
    def test_cli_flags_json_and_exit_mapping(self):
        args=[str(REPO/'doit'),'tg-say','--sender','cli','--label','[YOU]','--what','Ready','--real','Checked','--you','Read']
        for code in ('200','429','000'):
            result=subprocess.run(args,env=self.env|{'HTTP':code},capture_output=True,text=True)
            self.assertEqual(json.loads(result.stdout),{'state':'sent' if code=='200' else 'failed','http':int(code) or None})
            self.assertEqual(result.returncode==0,code=='200')
        args[-1]='12:00 UTC'
        result=subprocess.run(args,env=self.env,capture_output=True,text=True)
        self.assertEqual(json.loads(result.stdout),{'state':'refused','http':None});self.assertNotEqual(result.returncode,0)
        self.assertNotEqual(subprocess.run(args+['--unknown'],env=self.env,capture_output=True).returncode,0)
        self.assertEqual(len(self.calls()),3)
        with patch.object(tg,'_now',return_value=datetime.fromisoformat('2026-01-15T23:00:00+02:00')),patch('builtins.print') as output:
            self.assertEqual(tg.main(['--sender','cli','--label','[DAILY]','--what','Ready','--real','Checked','--you','Read']),0)
            self.assertEqual(json.loads(output.call_args.args[0]),{'state':'held','http':None})


if __name__ == '__main__':
    unittest.main()
