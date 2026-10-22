"""Operator summaries and Codex drill-downs use facts, never age alone."""
import contextlib
import datetime as dt
import http.client
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
from unittest import mock

import realm_operator as op
import realm_codex as codex

NOW = dt.datetime(2026, 10, 22, 12, tzinfo=dt.timezone.utc)


def event(kind, at=0, subject='L-spec-9001', **fields):
    return dict(type=kind, subject=subject, ts=(NOW + dt.timedelta(minutes=at)).isoformat(), **fields)


def rule(sid='L-spec-9001', name='build', **fields):
    return dict(spec=sid, rule=name, since=(NOW-dt.timedelta(minutes=31)).isoformat(), limit_min=7,
                overdue=True, reason_live=False, reason='condition met', action='dispatch-builder', **fields)


def shape(events=(), states=(), queue=(), rules=(), classified=(), priorities=None, **fields):
    return op.shape(events, states, queue, rules, classified, priorities or {}, NOW, **fields)


def test_eta_uses_completed_window_duration_not_queue_wait_and_limits_to_five():
    events = [event('window-opened', -80, 'win-1'), event('window-verified', -70, 'win-1', kept=['L-spec-8998']),
              event('window-opened', -60, 'win-2'), event('window-verified', -30, 'win-2', kept=['L-spec-8999']),
              event('window-opened', -10, 'unfinished'), event('window-opened', -2000, 'old'),
              event('window-verified', -1900, 'old', kept=[])]
    queue = [dict(spec=f'L-spec-{9001+i}', added_at=event('x', -20+i)['ts']) for i in range(7)]
    d = shape(events, queue=queue, titles={'L-spec-9001': 'Useful feature'})
    assert d['median_window_min'] == 20
    assert len(d['next']) == 5
    assert [(s['position'], s['eta_min']) for s in d['next']] == [(i, i*20) for i in range(1, 6)]
    assert d['next'][0]['title'] == 'Useful feature'
    assert shape(queue=queue)['next'][0]['eta_min'] is None


def test_plain_stages_and_live_requires_own_deployment_evidence():
    for state, expected in [('building','building'), ('built','checking'), ('grading','checking'),
                            ('graded','in review'), ('reviewing','in review'), ('shipped','shipped'),
                            ('shipped-owed-due','shipped'), ('closed-shipped','shipped'), ('closed-unbuilt','stopped'), ('accepted','live')]:
        assert op.stage(state) == expected
    assert op.stage('reviewing', position=3) == 'waiting to merge (position 3)'
    events = [event('shipped', -20, sha='abcdef'), event('deploy-landed', -10, 'another-project', sha='other')]
    d = shape(events, [('L-spec-9001','shipped')], priorities={'P':['L-spec-9001']})
    assert d['priorities'][0]['specs'][0]['stage'] == 'shipped'
    events.append(event('deploy-landed', -5, 'deploy', sha='abcdef'))
    assert shape(events, [('L-spec-9001','shipped')], priorities={'P':['L-spec-9001']})['priorities'][0]['specs'][0]['stage'] == 'live'


def test_overdue_hold_wave_migration_queue_and_active_work_are_never_stuck():
    ids = [f'L-spec-{i}' for i in range(9001, 9008)]
    rules = [rule(s) for s in ids]
    rules += [rule(ids[1], 'wave-held', blocker='L-spec-8990'),
              rule(ids[2], 'dependency-held', blocker='L-spec-8991'),
              rule(ids[3], 'migration-sequenced', blocker='L-spec-8992')]
    rules[5]['reason_live'] = True
    queue = [dict(spec=ids[4], added_at=event('x', -10)['ts'])]
    events = [event('window-opened', -10, 'open-window', specs=[ids[6]])]
    d = shape(events, [(s,'written') for s in ids], queue, rules)
    assert [s['spec'] for s in d['stuck']] == [ids[0]]
    assert d['stuck'][0]['limit_min'] == 7
    assert [s['stage'] for s in d['shelf']['held']] == ['waiting on L-spec-8990', 'waiting on L-spec-8991', 'waiting on L-spec-8992']
    assert len(d['shelf']['held']) == 3
    # Rules that have not reached their own limit never enter Actually stuck.
    rules[0]['overdue'] = False
    assert shape(events, [(s,'written') for s in ids], queue, rules)['stuck'] == []


def test_legacy_clean_review_queue_is_fifo_and_active_dispatch_is_not_merge_ready():
    classified = [dict(spec='L-spec-9001', bucket='queued', reason='awaiting merge window after review', since=event('x', -70)['ts']),
                  dict(spec='L-spec-9002', bucket='queued', reason='grader owns active dispatch L-grader-1', since=event('x', -80)['ts']),
                  dict(spec='L-spec-9003', bucket='queued', reason='in merge window win-1', since=event('x', -60)['ts'])]
    d = shape(classified=classified)
    assert [s['spec'] for s in d['next']] == ['L-spec-9001','L-spec-9003']
    assert d['next'][1]['position'] == 2
    # Explicit queue order is causal when two appends share a timestamp.
    tied=[dict(spec='L-spec-9003',added_at=event('x',-5)['ts']),dict(spec='L-spec-9001',added_at=event('x',-5)['ts'])]
    assert [s['spec'] for s in shape(queue=tied)['next']] == ['L-spec-9003','L-spec-9001']


def test_needs_you_uses_open_operator_escalations_only_and_respects_cross_subject_answers():
    events = [event('escalation-blocking', -20, owner='operator', why='Choose', _src='a:1'),
              event('decision', -10), event('question', -5, 'L-spec-9002', owner='operator', _src='a:2'),
              event('escalation-blocking', -20, 'L-spec-9003', owner='thinker', _src='a:3'),
              event('escalation-blocking', -20, 'L-spec-9004', owner='operator', _src='a:4'),
              event('escalation-blocking', -20, 'L-spec-9005', owner='operator', _src='a:5'),
              event('escalation-blocking', -5, 'L-spec-9005', owner='thinker', _src='a:6'),
              event('escalation-blocking', -1, 'L-spec-9006', owner='operator', why='Pick a date', _src='a:7')]
    assert [s['spec'] for s in shape(events, answered={'a:4'})['needs_you']] == ['L-spec-9006']


def test_today_uses_utc_unique_specs_and_queue_trend():
    events = [event('queue-add', -800), event('queue-remove', -10),
              event('window-verified', -5, 'win-1', kept=['L-spec-9001']),
              event('window-verified', -4, 'win-2', kept=['L-spec-9001']),
              event('queue-add', -3, 'L-spec-9002'), event('queue-add', -2, 'L-spec-9002')]
    q = [dict(spec='L-spec-9002', added_at=events[-1]['ts'])]
    assert shape(events, queue=q)['keeping_up'] == dict(merged=1, added=1, trend='steady', delta=0, queue=1)
    assert shape(events+[event('queue-remove', -1, 'L-spec-9002')])['keeping_up']['trend'] == 'shrinking'
    q.append(dict(spec='L-spec-9003', added_at=events[-1]['ts']))
    assert shape(events+[event('queue-add', -1, 'L-spec-9003')], queue=q)['keeping_up']['trend'] == 'growing'


def test_priority_config_seed_and_invalid_shape():
    config = pathlib.Path(__file__).resolve().parent.parent / 'realm_priorities.json'
    groups = op.load_priorities(config)
    assert groups['Listings'] == ['L-spec-1409','L-spec-1410']
    assert groups["Dina's tools"] == ['L-spec-1430']
    assert groups['GitHub CI fix'] == ['L-spec-1454']
    assert groups['Data connector for Claude'] == ['L-spec-1417','L-spec-1418','L-spec-1420','L-spec-1422']
    with tempfile.TemporaryDirectory() as folder:
        f=pathlib.Path(folder)/'bad.json'; f.write_text('{"P": "L-spec-9001"}')
        try: op.load_priorities(f)
        except ValueError: pass
        else: raise AssertionError('invalid config accepted')


def test_codex_today_outcomes_and_burndown_allowlist():
    assert codex.first_paragraph('# Title\nFirst prose without a blank line.\n\nSecond paragraph.') == 'First prose without a blank line.'
    with tempfile.TemporaryDirectory() as folder:
        home=pathlib.Path(folder); root=home/'.do-it/burndown'; briefs=root/'briefs';briefs.mkdir(parents=True)
        for name in ('w1','w2','unbriefed','private','linked-report'):
            (root/name).mkdir()
        (briefs/'w1-brief.md').write_text('## Hard rules\n\nDo not expose prompts.\n\n# W1: useful work\n\nprivate body')
        (briefs/'w2.md').write_text('# W2: still working')
        (briefs/'private.md').write_text('# SHOULD_NOT_READ')
        (root/'private/REPORT.md').write_text('SECRET_MEETING_BODY')
        (root/'linked-dir').symlink_to(root/'private', target_is_directory=True)
        (briefs/'linked-dir.md').write_text('# SHOULD_NOT_READ')
        (briefs/'linked-report.md').write_text('# Linked report')
        (root/'linked-report/REPORT.md').symlink_to(root/'private/REPORT.md')
        (root/'w1/REPORT.md').write_text('# Result\n\nThe operator can see progress. API_KEY=sk-testcredential\n\nPRIVATE_SECOND_PARAGRAPH')
        rows=[event('spawn-started', -10, spawn='L-grader-9001', role='grader', backend='codex'),
              event('spawn-done', -5, spawn='L-grader-9001', status='DONE'),
              event('spawn-started', -1440, spawn='L-builder-9002', backend='codex'),
              event('spawn-started', -10, spawn='L-reviewer-9003', backend='claude')]
        sessions=home/'.codex/sessions/2026/10/22';sessions.mkdir(parents=True)
        private_session=sessions/'rollout-private.jsonl'
        private_session.write_text(json.dumps({'type':'session_meta','payload':{'id':'private-session','cwd':str(home/'.do-it/private')}})+'\n'+json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'# SECRET_MEETING_BODY'}]}})+'\n')
        import os
        os.utime(private_session,(NOW.timestamp(),NOW.timestamp()))
        d=codex.payload(rows, home=home, now=NOW.timestamp(), processes=set(), windows={})
        assert [(s['role'],s['subject'],s['outcome']) for s in d['seats_today']] == [('grader','L-spec-9001','DONE')]
        workers={s['name']:s for s in d['workers']}
        assert set(workers) == {'w1','w2','linked-report'}
        assert workers['w1']['title'] == 'W1: useful work'
        assert workers['w1']['state'] == 'done' and 'operator can see progress' in workers['w1']['summary']
        assert workers['w2']['state'] == 'running'
        encoded=json.dumps(d)
        for marker in ('SECRET_MEETING_BODY','PRIVATE_SECOND_PARAGRAPH','sk-testcredential','private body',str(home)):
            assert marker not in encoded, marker


def test_read_only_http_feeds_require_existing_auth_and_inline_modules_are_assembled():
    import realm
    server=realm.http.server.ThreadingHTTPServer(('127.0.0.1',0),realm.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    with mock.patch.object(realm.Handler,'key','test-key'), mock.patch.object(realm,'operator_payload',return_value={'next':[]}), mock.patch.object(realm,'codex_payload',return_value={'seats_today':[],'workers':[]}):
        thread.start()
        try:
            for path, expected in [('/api/operator','next'),('/api/codex','workers')]:
                c=http.client.HTTPConnection(*server.server_address,timeout=5)
                c.request('GET',path);r=c.getresponse();assert r.status==401;r.read();c.close()
                c=http.client.HTTPConnection(*server.server_address,timeout=5)
                c.request('GET',path,headers={'X-Realm-Key':'test-key'});r=c.getresponse()
                assert r.status==200 and expected in json.loads(r.read());c.close()
            body=codex.page_bytes(realm.PAGE)
            assert b'/*__OPERATOR_JS__*/' not in body and b'/*__CODEX_JS__*/' not in body
            assert b'const Operator = ' in body and b'const Codex = ' in body
        finally:
            server.shutdown();thread.join();server.server_close()



def test_operator_payload_reads_real_fold_and_configured_rule_limit_in_an_isolated_root():
    import os
    import realm
    import realm_ledger
    with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as stack:
        root=pathlib.Path(folder);(root/'events').mkdir();(root/'content').mkdir()
        (root/'events/L-planner-fixture.jsonl').write_text(''.join(json.dumps(event('spec-written', -15, sid))+'\n' for sid in ('L-spec-9001','L-spec-9002')))
        (root/'events/L-thinker-fixture.jsonl').write_text(json.dumps(event('dependency-hold', -14, 'L-spec-9002', waits_for='L-spec-9001', why='Migration sequence'))+'\n')
        (root/'content/L-spec-9001.md').write_text('# Human title\n\nUseful feature.')
        rules=root/'rules.toml';rules.write_text((realm.HERE.parent/'lane_rules.toml').read_text().replace('limit_min = 30','limit_min = 7'))
        stack.enter_context(mock.patch.dict(os.environ, {'DOIT_ROOT':str(root),'DOIT_LIVE_REPO':str(realm.HERE.parent),'DOIT_LANE_RULES':str(rules)}))
        stack.enter_context(mock.patch.object(realm,'ROOT',root))
        stack.enter_context(mock.patch.object(realm,'EVENTS',root/'events'))
        stack.enter_context(mock.patch.object(realm,'LEDGER',realm_ledger.Ledger()))
        stack.enter_context(mock.patch.object(realm,'_OPERATOR',{'key':None,'value':None,'lock':threading.Lock()}))
        d=realm.operator_payload(NOW)
        assert d['error'] is None,d
        assert [(r['spec'],r['limit_min']) for r in d['stuck']] == [('L-spec-9001',7)],d['stuck']
        assert [r['stage'] for r in d['shelf']['held']] == ['waiting on L-spec-9001']
        assert d['shelf']['ready'][0]['title']=='Human title'



def test_spec_titles_prefer_descriptive_headings_to_ids_and_slug_units():
    import realm
    with tempfile.TemporaryDirectory() as folder, mock.patch.object(realm,'ROOT',pathlib.Path(folder)):
        content=pathlib.Path(folder)/'content';content.mkdir()
        f=content/'L-spec-9001.md'
        f.write_text('---\nspec_id: L-spec-9001\nunit: mechanical-slug\n---\n# L-spec-9001 — A useful title for the operator\n\nUseful paragraph.')
        assert realm.spec_text('L-spec-9001')['title']=='A useful title for the operator'



def test_hosted_and_saved_snapshots_carry_the_same_operator_projection():
    # Exercise the existing push builder with the outbound call replaced; no
    # network or service is contacted. Its sleep ends this one-cycle fixture.
    import realm
    class EndCycle(Exception): pass
    with mock.patch.object(realm,'bootstrap',return_value={}), mock.patch.object(realm,'operator_payload',return_value={'next':[]}), \
         mock.patch.object(realm,'_post') as post, mock.patch.object(realm.time,'sleep',side_effect=EndCycle):
        try: realm.push_forever('https://example.invalid','fixture-token',24)
        except EndCycle: pass
    assert post.call_count==2
    for call in post.call_args_list:
        assert call.args[2]['operator']=={'next':[]}


def test_operator_js_five_rows_rounded_etas_titles_hover_and_escaping():
    here=pathlib.Path(__file__).resolve().parent
    data=shape([event('window-opened', -30, 'win'),event('window-verified', -10, 'win', kept=[])],
               [('L-spec-9001','written')], [dict(spec='L-spec-9002',added_at=event('x',-5)['ts'])],
               priorities={'Listings':['L-spec-9001']},titles={'L-spec-9002':'A useful feature','L-spec-9001':'<img onerror=bad>'})
    script="const assert=require('node:assert/strict');\n" + (here/'realm_operator.js').read_text()
    script+='''
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const d=DATA;
const h=Operator.summaryHtml(d);
assert.equal((h.match(/class="know-row/g)||[]).length,5);
assert(h.includes('title="L-spec-9002 · A useful feature"'));
assert(h.includes('A useful feature (')===false); // title is a real link
assert(h.includes('about 20 min'));
assert(!h.includes('<img'));assert(h.includes('&lt;img'));
assert(h.includes('Needs you:</b> nothing'));
assert(h.includes('Actually stuck:</button> nothing'));
for(const [n,want] of [[null,'ETA unavailable'],[39.3,'about 40 min'],[127,'about 2 h'],[3000,'about 2 days']])assert.equal(Operator.duration(n),want);
console.log(h.replace(/<[^>]*>/g,'').replace(/&lt;/g,'<').replace(/&gt;/g,'>'));
'''.replace('const d=DATA;', 'const d='+json.dumps(data)+';')
    p=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
    assert p.returncode==0,p.stderr
    print(p.stdout,end='')


if __name__ == '__main__':
    for name,fn in list(globals().items()):
        if name.startswith('test_'):
            fn();print('ok',name)
