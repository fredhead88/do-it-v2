// SD2 events -> production fold/classifier/shape. No live ledger or bucket stubs.
const {test,expect}=require('playwright/test');
const {spawn}=require('node:child_process');
const {mkdtempSync,mkdirSync,writeFileSync}=require('node:fs');
const {join,resolve}=require('node:path');
const {tmpdir}=require('node:os');
const {createHash}=require('node:crypto');
const root=resolve(__dirname,'..');
const evidence=process.env.STUCK_EVIDENCE_DIR||mkdtempSync(join(tmpdir(),'realm-stuck-'));
mkdirSync(evidence,{recursive:true});
console.log('AC8 evidence: '+evidence);
test.setTimeout(120_000);
const python=String.raw`
import sys,datetime as dt,http.server,urllib.parse,json
sys.path.insert(0,'src')
import test_realm as fixture
import realm,realm_glance as g
from test_realm_stuck import fixture_payload,fixture_events
realm.sys_signals=lambda: {}
realm.maker_index=lambda *a: {}
realm.codex_payload=lambda *a: {'sessions':[], 'quota':{}}
now=dt.datetime.now(dt.timezone.utc)
groups={'Overdue':['L-spec-9001','L-spec-9002'],'Holds':['L-spec-9003','L-spec-9004','L-spec-9005'],
        'Audit':['L-spec-9006'],'Retry':['L-spec-9007'],'Repair':['L-spec-9008']}
# Seed bootstrap and detail events in the isolated root.
by_actor={}
for e in fixture_events(now):
 e=dict(e);actor=e.pop('actor');e.pop('_src',None);by_actor.setdefault(actor,[]).append(e)
for actor,rows in by_actor.items():fixture.w('L-'+actor+'-fixture.jsonl',rows)
def payload(scenario):
 events,folded,lane,stuck=fixture_payload(now,scenario)
 glance=g.shape(events,[(s,v['state']) for s,v in folded[0].items()],[],lane,groups,now,stuck=stuck)
 return glance,[(r['spec'],r['state'],r['bucket'],r['hold_kind'],r['due_since'],r['reason']) for r in lane]
glance,_=payload('base')
realm.glance_payload=lambda *a: glance
d=realm.bootstrap(24,states=[],goals=[],proving=[],now=now);d['live']=True
class Handler(realm.Handler):
 def do_GET(self):
  u=urllib.parse.urlparse(self.path);path=u.path
  scenario=urllib.parse.parse_qs(u.query).get('scenario',['base'])[0]
  if path=='/api/bootstrap': self._json(dict(d,glance=payload(scenario)[0]));return
  if path=='/api/glance': self._json(payload(scenario)[0]);return
  if path=='/fixture':
   glance,rows=payload(scenario);self._json(dict(glance=glance,provenance=rows));return
  if path=='/api/since': self._json({'seq':0,'ledger':[],'claims':[]});return
  if path.startswith('/api/'): self._json({});return
  super().do_GET()
httpd=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
print(httpd.server_address[1],flush=True);httpd.serve_forever()
`;
let server,base;
test.beforeAll(async()=>{
 server=spawn('/usr/bin/python3',['-u','-c',python],{cwd:root,stdio:['ignore','pipe','pipe']});
 base=await new Promise((done,fail)=>{let out='';server.stdout.on('data',c=>{out+=c;const m=/^(\d+)\n/.exec(out);if(m)done('http://127.0.0.1:'+m[1]);});server.on('exit',c=>fail(Error('fixture exit '+c)));server.stderr.on('data',c=>process.stderr.write(c));});
});
test.afterAll(()=>server?.kill());
test.beforeEach(async({page})=>{
 await page.context().tracing.start({screenshots:true,snapshots:true});
 await page.emulateMedia({reducedMotion:'reduce'});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
});
test.afterEach(async({page},info)=>{
 const name=info.title.replace(/[^a-z0-9-]/gi,'_');
 await page.screenshot({path:join(evidence,name+'.png'),fullPage:true});
 await page.context().tracing.stop({path:join(evidence,name+'.trace.zip')});
});
async function open(page,width=1440,height=900){
 await page.setViewportSize({width,height});await page.goto(base);
 await expect(page.locator('#status')).toBeHidden();
 await expect(page.locator('#h-stuck b')).toHaveText('2');
 const response=await page.request.get(base+'/fixture');const body=await response.text();
 writeFileSync(join(evidence,'fixture-response.json'),JSON.stringify({url:response.url(),status:response.status(),body_sha256:createHash('sha256').update(body).digest('hex'),body_excerpt:JSON.parse(body)},null,2));
}
async function scenario(page,name){
 const response=await page.request.get(base+'/fixture?scenario='+encodeURIComponent(name));const d=await response.json();
 console.log(name,JSON.stringify(d.provenance),d.glance.stuck.count,d.glance.stuck.held.length);
 await page.evaluate(d=>{DATA.glance=d.glance;renderGlance();mRender();rerenderPanel();},d);
 return d;
}
function noJargon(text){
 expect(text).not.toMatch(/\bSTUCK\b|\bheld\b/); // Exact bucket values; natural headings are permitted.
 expect(text).not.toMatch(/\b(stalls|bucket|dependency-held|wave-held|migration-sequenced|conflict-rework|waiting-on-prereq|capability-hold|audit-wait|retry-wait|in-flight)\b/i);
}
test('AC8 base counts, responsive tiles, details and plain states',async({page})=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 for(const width of [1440,701]){
  await open(page,width,width===1440?900:800);
  const tile=page.locator('#h-stuck');await expect(tile).toBeVisible();
  noJargon(await tile.innerText());
  const box=await tile.boundingBox();expect(box.x).toBeGreaterThanOrEqual(0);expect(box.x+box.width).toBeLessThanOrEqual(width);
  await expect(page.locator('#glance-sentence')).toContainText('2 specs are stuck');
  await expect(page.locator('#stuck')).toHaveText('2 stuck');
  await tile.click();await expect(page.locator('#p-body .stuck-row')).toHaveCount(5);
  await expect(page.locator('#p-body h2')).toHaveText('Stuck right now · 2');
  await expect(page.locator('#p-body h3')).toHaveText('Held on purpose · 3');
  for(const kind of ['Operator hold','Decision hold','Dependency hold'])await expect(page.locator('#p-body')).toContainText(kind);
  noJargon(await page.locator('#p-body').innerText());
  await expect(page.locator('#p-body')).toContainText('fixture-project');
  await expect(page.locator('#p-body')).toContainText('1m');
  await page.screenshot({path:join(evidence,'list-'+width+'.png'),fullPage:true});
  await page.locator('#p-body .stuck-row a').first().click();await expect(page.locator('#p-body h2')).toContainText('L-spec-9001');
  await page.locator('#p-close').click();
  await page.locator('#glance-toggle').click();
  for(const label of ['audit pending','retry scheduled','building'])await expect(page.locator('#glance-lines')).toContainText(label);
  noJargon(await page.locator('#glance-lines').innerText());
 }
 await open(page,1440,900);
 const top=await page.locator('#strip').boundingBox();expect(top.height).toBeLessThanOrEqual(120);
 await open(page,390,844);await page.locator('#mnav [data-t="now"]').click();
 await expect(page.locator('#mview .m-tile[data-cell="h-stuck"] b')).toHaveText('2');
 noJargon(await page.locator('#mview .m-tile[data-cell="h-stuck"]').innerText());
 expect(errors).toEqual([]);
});
test('AC8 named sources only count through their lane',async({page})=>{
 await open(page);await page.locator('#h-stuck').click();
 for(const [name,count,held] of [['escalation',0,0],['window',0,0],['held-escalation',0,1],['expired-decision',1,0],['dead-attempt',1,0],['owed',0,0],['owed-unavailable',2,3],['base+escalation',2,3],['base+window',2,3],['base+held-escalation',2,4]]){
  const d=await scenario(page,name);expect(d.glance.stuck.count).toBe(count);expect(d.glance.stuck.held.length).toBe(held);
  await expect(page.locator('#h-stuck b')).toHaveText(String(count));
  await expect(page.locator('#stuck')).toHaveText(count+' stuck');
  await expect(page.locator('#p-body h2')).toContainText('· '+count);
  await expect(page.locator('#p-body h3')).toContainText('· '+held);
  noJargon(await page.locator('#p-body').innerText());
 }
 await scenario(page,'lane-unavailable');
 await expect(page.locator('#h-stuck b')).toHaveText('stuck list unavailable');
 await expect(page.locator('#p-body')).toContainText('stuck list unavailable');
});
test('AC8 ledger title reason and hold text stay escaped',async({page})=>{
 await open(page);await page.locator('#h-stuck').click();
 await expect(page.locator('#p-body')).toContainText('<img src=x onerror="window.__x=1">');
 await expect(page.locator('#p-body')).toContainText('<script>window.__x=1</script>');
 await expect(page.locator('#p-body')).toContainText('&amp;');
 await expect(page.locator('#p-body img,#p-body script')).toHaveCount(0);
 expect(await page.evaluate(()=>window.__x)).toBeUndefined();
 for(const row of await page.locator('#p-body .stuck-row').all()){
  await expect(row).toContainText('<img');
  noJargon(await row.innerText());
 }
});
