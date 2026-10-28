"""Scratch-only regressions for Codex identity, usage and shared inspection."""
import copy
import datetime as dt
import hashlib
import http.server
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

import realm_codex as c

NOW = 1793188800.0
START = c.stamp(NOW - 120)


def start(**extra):
    return dict(type='spawn-started', ts=START, spawn='L-builder-7',
                subject='L-spec-7', role='builder', backend='codex', **extra)


def worker(**extra):
    return dict(id='worker-7', kind='worker', subject='Task', role='worker',
                started=c.stamp(NOW - 110), last=c.stamp(NOW), state='live', **extra)


def rollout(home, ident='worker-7', cwd=None, age=0, heading='Task'):
    folder = home / '.codex/sessions/2026/10/28'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ('rollout-' + ident + '.jsonl')
    rows = [dict(type='session_meta', payload=dict(id=ident, cwd=str(cwd or home), timestamp=c.stamp(NOW-110))),
            dict(type='response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text='# '+heading+'\nPRIVATE_BODY_SENTINEL')])),
            dict(type='event_msg', payload=dict(type='token_count', info=dict(total_token_usage=dict(input_tokens=100, cached_input_tokens=50, output_tokens=20, reasoning_output_tokens=10))))]
    path.write_text('\n'.join(map(json.dumps, rows)) + '\n{"partial":')
    os.utime(path, (NOW-age, NOW-age))
    return path


def signed(data):
    """Read-only HTTP evidence, never real ledger or rollout contents."""
    body = json.dumps(data, sort_keys=True).encode()
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever); thread.start()
    try:
        with urllib.request.urlopen('http://127.0.0.1:%s/fixture' % server.server_port) as response:
            received = response.read()
            print(json.dumps(dict(url=response.url, status=response.status, body_sha256=hashlib.sha256(received).hexdigest(), body_excerpt=received.decode()[:1200])))
    finally:
        server.shutdown(); server.server_close(); thread.join()


class CodexDetail(unittest.TestCase):
    def test_1669_ac1_join(self):
        ledger = c.ledger_sessions([start(), dict(type='spawn-done', ts=c.stamp(NOW), spawn='L-builder-7', session='worker-7', input_tokens=100, output_tokens=20)])
        original = copy.deepcopy(ledger)
        w = worker(_worktree='/different/project')
        pair = c.join(ledger, [w])
        self.assertEqual([(s['id'], s['spawn'], s['worker_id']) for s in pair], [('L-builder-7', 'L-builder-7', 'worker-7')])
        self.assertEqual(ledger, original)
        self.assertEqual(c.join(ledger+ledger, [w,w]), pair)
        print('explicit different worktree; duplicate observations:', [(s['id'],s['worker_id']) for s in pair])
        with tempfile.TemporaryDirectory() as folder:
            home = pathlib.Path(folder)
            rollout(home)
            ancestor = (socket.gethostname(), 101, '111')
            process_tree = {101: dict(ppid=1, start='111'), 102: dict(ppid=101, start='222', sessions=['worker-7'])}
            cases = {
                'verified ancestry': (ancestor, process_tree, 1),
                'wrong host': (('other-host',101,'111'), process_tree, 2),
                'reused pid': ((socket.gethostname(),101,'999'), process_tree, 2),
                'inaccessible tree': (ancestor, {}, 2),
                'unrelated process': ((socket.gethostname(),200,'111'), process_tree, 2),
            }
            for role in ['spec-writer', 'probe', 'research']:
                for name, (identity, tree, count) in cases.items():
                    row = start(waiter_host=identity[0], waiter_pid=identity[1], waiter_proc_start=identity[2]); row['role'] = role
                    projection = c.ledger_sessions([row]); untouched = copy.deepcopy(projection)
                    with mock.patch.object(c, '_read_process_tree', return_value=tree):
                        a = c.payload([row], home=home, now=NOW, windows={})
                        b = c.payload([], home=home, now=NOW, windows={}, ledger_projection=projection)
                    self.assertEqual(a,b); self.assertEqual(projection,untouched)
                    self.assertEqual(len(a['sessions']),count, (role,name,a))
                    print(role, name, [(s['id'],s.get('worker_id')) for s in a['sessions']])
            # Two live writers in the same cwd have independent waiter identities.
            rollout(home, ident='worker-8')
            second = dict(start(waiter_host=socket.gethostname(), waiter_pid=201, waiter_proc_start='333'), spawn='L-spec-writer-8', role='spec-writer')
            first = dict(start(waiter_host=socket.gethostname(), waiter_pid=101, waiter_proc_start='111'), spawn='L-spec-writer-7', role='spec-writer')
            tree = dict(process_tree, **{})
            tree.update({201:dict(ppid=1,start='333'),202:dict(ppid=201,start='444',sessions=['worker-8'])})
            with mock.patch.object(c, '_read_process_tree', return_value=tree):
                pairs = c.payload([first,second],home=home,now=NOW,windows={})['sessions']
            self.assertEqual([(s['id'],s['worker_id']) for s in pairs],[('L-spec-writer-7','worker-7'),('L-spec-writer-8','worker-8')])
            print('two simultaneous shared-cwd writers',[(s['id'],s['worker_id']) for s in pairs])
            # A common waiter ancestor cannot distinguish two observations.
            tree[202]['ppid']=101
            with mock.patch.object(c, '_read_process_tree', return_value=tree):
                ambiguous=c.payload([first],home=home,now=NOW,windows={})['sessions']
            self.assertEqual(len(ambiguous),3)
            print('ambiguous ancestry',[(s['id'],s['worker_id']) for s in ambiguous])
            for changes in [dict(ts=None),dict(ts=c.stamp(NOW-600))]:
                row=dict(first,**changes)
                with mock.patch.object(c,'_read_process_tree',return_value=process_tree):
                    self.assertEqual(len(c.payload([row],home=home,now=NOW,windows={})['sessions']),3)
            (home/'.codex/sessions/2026/10/28/rollout-worker-8.jsonl').unlink()
            row = start(worktree=str(home/'tree'))
            l = c.ledger_sessions([row]); w = worker(_worktree=str(home/'tree'))
            for name, ls, ws, count in [
                ('unique worktree', l,[w],1), ('ledger only',l,[],1), ('worker only',[],[w],1),
                ('ambiguous retry',l+[dict(l[0],id='retry',spawn='retry')],[w],3),
                ('two workers',l,[w,dict(w,id='other')],3),
                ('no start',l,[dict(w,started=None)],2),
                ('outside five minutes',l,[dict(w,started=c.stamp(NOW+300))],2),
                ('same basename different project',l,[dict(w,_worktree=str(home/'other/tree'))],2),
                ('mismatched explicit', [dict(l[0],session='another')],[w],2),
                ('missing evidence',c.ledger_sessions([start()]),[worker()],2),
            ]:
                before = copy.deepcopy((ls,ws)); result=c.join(ls,ws)
                self.assertEqual(len(result),count,name); self.assertEqual((ls,ws),before)
                print(name,[(s['id'],s.get('worker_id')) for s in result])
            retry=dict(l[0],id='earlier',spawn='earlier',started=c.stamp(NOW-900),last=c.stamp(NOW-600),state='ended',session='earlier-worker')
            old=worker();old.update(id='earlier-worker',started=c.stamp(NOW-890),state='ended',_worktree=str(home/'tree'))
            reused=c.join([retry]+l,[old,w])
            self.assertEqual([(s['id'],s['worker_id']) for s in reused],[('L-builder-7','worker-7'),('earlier','earlier-worker')])
            print('reused worktree distinct retries',[(s['id'],s['worker_id']) for s in reused])
            live=c.join(l,[w]); final=c.join([dict(l[0],session='worker-7',state='ended')],[w])
            self.assertEqual(live[0]['id'],final[0]['id'])
        signed(dict(sessions=pair))

    def test_1669_ac2_completion(self):
        import realm
        cases=[({'input_tokens':100,'output_tokens':20},120), ({'tokens':{'input':100,'output':20,'cached_input':70,'reasoning':10}},120),
               ({'tokens':123},123),({'subagent_tokens':42},42),({'input_tokens':0,'output_tokens':0},0),({},None),
               ({'input_tokens':True,'output_tokens':-1,'tokens':float('inf')},None)]
        for fields,total in cases:
            done=dict(type='spawn-done',spawn='L-builder-7',ts=c.stamp(NOW),session='worker-7',status='DONE',backend='codex',**fields)
            rows=[done,dict(type='build-done',spawn='L-builder-7',ts=c.stamp(NOW-5),status='DONE'),start()]
            entry=c.ledger_sessions(rows)[0]
            self.assertEqual(entry['tokens'],total); self.assertEqual(realm.compact(done,'builder').get('tok'),total)
            self.assertEqual(entry['outcome'],'done'); self.assertEqual(c.join([entry],[worker()])[0]['state'],'ended')
            print('usage',fields,'=>',entry['input_tokens'],entry['output_tokens'],entry['tokens'])
        for ty,status,outcome in [('build-done','BLOCKED','blocked'),('build-blocked',None,'blocked'),('spawn-failed',None,'failed'),('spawn-stale',None,'stale'),('backend-fallback',None,'fallback'),('spec-written',None,'written')]:
            stop=dict(type=ty,ts=c.stamp(NOW-2),spawn='L-builder-7',subject='L-spec-7',status=status,reason='fixture reason')
            rows=[start(),stop,dict(type='spawn-done',spawn='L-builder-7',ts=c.stamp(NOW),status='DONE')]
            entry=c.ledger_sessions(rows)[0]; self.assertEqual(entry['outcome'],outcome)
            print(ty,entry['outcome'],entry['reason'])
        signed(dict(sessions=c.ledger_sessions([start(),dict(type='spawn-done',spawn='L-builder-7',ts=c.stamp(NOW),input_tokens=100,output_tokens=20,status='DONE')])))

    def test_1669_ac3_activity(self):
        now=dt.datetime.fromtimestamp(NOW,dt.timezone.utc)
        for role,phrase in [('builder','building'),('spec-writer','writing the spec'),('spec-auditor','auditing the spec'),('grader','grading'),('reviewer','reviewing'),('probe','probing'),('research','working')]:
            entry=dict(role=role,state='live',last=c.stamp(NOW)); before=copy.deepcopy(entry)
            self.assertEqual(c.activity(entry,now),phrase); self.assertEqual(c.activity(entry,now),phrase);self.assertEqual(entry,before)
            print(role,phrase)
        for state,outcome,reason,phrase in [('idle',None,None,'waiting on the model'),('ended',None,None,'inactive; outcome unavailable'),('ended','done',None,'finished and departing'),('ended','written',None,'finished and departing'),('ended','blocked','why','blocked: why'),('ended','failed',None,'failed: reason unavailable'),('fallback','fallback','why','fell back: why')]:
            self.assertEqual(c.activity(dict(state=state,outcome=outcome,reason=reason),now),phrase)
        with tempfile.TemporaryDirectory() as folder:
            home=pathlib.Path(folder); rollout(home)
            with mock.patch.object(c,'activity',wraps=c.activity) as spy:
                result=c.payload([start()],home=home,now=NOW,processes={'worker-7'},windows={})
                self.assertEqual(spy.call_count,2)
                self.assertTrue(all(call.args[1]==now and call.args[1].tzinfo is dt.timezone.utc for call in spy.call_args_list))
            for age,state in [(179,'live'),(180,'idle'),(1799,'idle'),(1800,'ended')]:
                path=rollout(home,age=age); self.assertEqual(c.read_worker(path,home,NOW,{'worker-7'}, {})[0]['state'],state)
            signed(result)
        # Execute the complete production module and real delivery functions.
        page=(pathlib.Path(__file__).parent/'realm.html').read_text()
        module=(pathlib.Path(__file__).parent/'realm_codex.js').read_text()
        functions='\n'.join(line for line in page.splitlines() if line.startswith(('function applyBatch(', 'function applySnapshot(')))
        script=r"""
const assert=require('node:assert/strict');
const element=()=>({style:{},dataset:{},setAttribute(){},appendChild(){},after(){},remove(){},addEventListener(){},getContext(){return {};}});
const document={createElement:element,head:element()},window={},cv=element(),PLACES={},$=()=>element();
let DATA={codex:{}},S={quiet:false,actors:new Map()},T0=Date.parse('2030-01-01'),seq=0,EVENTS=[],LIVE=false,lastPush='',T1=0;
const toMs=x=>Date.parse(x)||0,roleOf=()=>'',applyViews=()=>{},applyThroughput=()=>{},insertEvent=()=>{},REDUCED=true;
const spawnActor=()=>{},finishActor=()=>{},fmtDur=()=>'',openPanel=()=>{};
"""+module+'\n'+functions+r"""
const live={id:'spawn',kind:'doit-build',subject:'L-spec-7',role:'builder',state:'live',started:'2026-10-28T11:58:00Z',activity:'building'};
Codex.load({sessions:[live]});
const row=()=>Codex.sessionExtra(live).refresh();
assert(row().some(([k,v])=>k==='Activity'&&v==='building'));
applyBatch({codex:{sessions:[{...live,state:'idle',activity:'waiting on the model'}]},ledger:[]});
assert(row().some(([k,v])=>k==='Activity'&&v==='waiting on the model'));
applySnapshot({events:[],codex:{sessions:[{...live,state:'ended',outcome:'blocked',reason:'fixture stop',activity:'blocked: fixture stop'}]}});
assert(row().some(([k,v])=>k==='Activity'&&v==='blocked: fixture stop'));
Codex.event({ty:'spawn-done',sp:'spawn',st:'DONE',tok:120,t:'2026-10-28T12:00:00Z'});
assert(row().some(([k,v])=>k==='Outcome'&&v==='blocked'));
assert(row().some(([k,v])=>k==='Tokens'&&v===120));
Codex.reset();assert(row().some(([k,v])=>k==='Activity'&&v==='blocked: fixture stop'));
console.log('1669 AC3 production JS load, batch, snapshot, terminal, replay/reset PASS');
"""
        result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        print(result.stdout,end='')

    def test_1669_ac4_allowlist(self):
        with tempfile.TemporaryDirectory() as folder:
            home=pathlib.Path(folder); cwd=home/'project'
            rollout(home,cwd=cwd,heading='Safe <heading>')
            for count,lines in [(1,['codex-one']), (2,['codex-one']), (2,['codex-one','codex-two']), (2,['codex-two','codex-one'])]:
                if count==2:rollout(home,ident='worker-8',cwd=cwd)
                output='\n'.join(name+'\t'+str(cwd) for name in lines)
                with mock.patch.object(c.subprocess,'run',return_value=mock.Mock(stdout=output)):
                    windows=c.tmux_windows()
                result=c.payload([],home=home,now=NOW,processes={'worker-7','worker-8'},windows=windows)
                for session in result['sessions']:
                    self.assertEqual(session.get('tmux'), 'codex-one' if count==1 else None)
                serialized=json.dumps(result)
                for forbidden in [str(home),'PRIVATE_BODY_SENTINEL','_ancestor','_waiter','_worktree']:
                    self.assertNotIn(forbidden,serialized)
                allowed={'id','worker_id','spawn','session','kind','role','subject','state','started','last','tokens','turns','input_tokens','output_tokens','token_breakdown','activity','outcome','reason','cwd','tmux'}
                for s in result['sessions']:self.assertLessEqual(set(s),allowed)
                print('tmux ownership',count,len(lines),'PASS fields',sorted(result['sessions'][0]))
            malformed=[None,[],{'type':[],'spawn':{}},dict(start(),ts='https://example.invalid/private'),dict(type='spawn-done',spawn={},ts='bad')]
            result=c.payload(malformed,home=home,now=NOW,processes=set(),windows={})
            serialized=json.dumps(result)
            self.assertNotIn('example.invalid',serialized)
            # Only the permitted heading and capped reason may contribute free text.
            path=home/'.codex/sessions/2026/10/28/rollout-worker-7.jsonl'
            with path.open('a') as out:
                out.write('\n'+json.dumps(dict(type='response_item',payload=dict(type='function_call',arguments='TOOL_OUTPUT_SENTINEL https://example.invalid/private /private/fixture sk-syntheticfixturesecret'))))
                out.write('\n'+json.dumps(dict(type='event_msg',payload=dict(type='other',text='SECRET_BODY_SENTINEL'))))
            serialized=json.dumps(c.payload([],home=home,now=NOW,processes=set(),windows={}))
            for sentinel in ['TOOL_OUTPUT_SENTINEL','SECRET_BODY_SENTINEL','example.invalid','sk-syntheticfixturesecret','/private/fixture']:
                self.assertNotIn(sentinel,serialized)
            signed(dict(fields=sorted(result['sessions'][0]),privacy='PASS'))

    def test_1669_ac5_panel(self):
        import test_realm
        test_realm._spec_panel_node(r"""
DATA.specs['L-spec-7'].intent='Make <intent> readable';
const old={id:'old',kind:'doit-build',subject:'L-spec-7',role:'builder',outcome:'blocked',reason:'unsafe <reason>',input_tokens:100,output_tokens:20,tokens:120,last:'2026-10-25T01:00:00Z'};
const recent={...old,id:'new',reason:'new run',last:'2026-10-25T02:00:00Z'};
DATA.codex.sessions=[old,recent]; builds.set('old',{session:old}); inspect('old');
for(const text of ['Shared title','Make &lt;intent&gt; readable','L-charter-0064','builder','LATEST EVENTS','blocked','unsafe &lt;reason&gt;','Input tokens','Output tokens','100','20','120'])assert(panel.includes(text),text);
assert(!panel.includes('new run'));assert(panelView.html().includes('unsafe &lt;reason&gt;'));
old.reason='updated historical run';builds.set('old',{session:old});assert(panelView.html().includes('updated historical run'));
openSubject('L-spec-7');assert(panel.includes('new run'));
S.actors.set('actor',{subject:'L-spec-7',role:'builder',start:0});openActor('actor');assert(panel.includes('ACTOR FACTS'));assert(panel.includes('Make &lt;intent&gt; readable'));
workers.set('standalone',{session:{id:'standalone',kind:'worker',subject:'Task <heading>',last:'last-seen'}});inspect('standalone');
for(const text of ['Task &lt;heading&gt;','unavailable','last-seen'])assert(panel.includes(text),text);
assert(!panel.includes('CRITERIA'));
builds.set('charter',{session:{id:'charter',kind:'doit-build',subject:'L-charter-0064',role:'probe'}});S.subjectEvents.set('L-charter-0064',[{ty:'fixture-story'}]);inspect('charter');assert(panel.includes('fixture-story'));assert(panel.includes('probe'));
console.log('1669 AC5 production shared panel PASS');
""")


if __name__=='__main__':
    names=unittest.defaultTestLoader.getTestCaseNames(CodexDetail)
    if len(sys.argv)>2 or (len(sys.argv)==2 and sys.argv[1] not in names):
        sys.exit('unknown test selector')
    suite=unittest.TestSuite(CodexDetail(n) for n in ([sys.argv[1]] if len(sys.argv)==2 else names))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() and result.testsRun else 1)
