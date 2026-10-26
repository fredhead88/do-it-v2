// Existing Playwright installation, isolated Realm fixture, no live ledger.
const {test,expect}=require('playwright/test');
const {spawn}=require('node:child_process');
const {mkdtempSync,writeFileSync}=require('node:fs');
const {join,resolve}=require('node:path');
const {tmpdir}=require('node:os');
const {createHash}=require('node:crypto');
const root=resolve(__dirname,'..');
const evidence=process.env.STUCK_EVIDENCE_DIR||mkdtempSync(join(tmpdir(),'realm-stuck-'));
test.setTimeout(120_000);
const python=String.raw`
import sys,datetime as dt,http.server,urllib.parse
sys.path.insert(0,'src')
import test_realm as fixture
import realm
realm.sys_signals=lambda: {}
realm.maker_index=lambda *a: {}
realm.glance_payload=lambda *a: {}
realm.codex_payload=lambda *a: {'sessions':[], 'quota':{}}
now=dt.datetime.now(dt.timezone.utc)
def row(sid,hours,kind='lane'):
 return dict(spec=sid,subject=sid or 'access-question',title='Title '+str(sid),project='fixture-project',since=(now-dt.timedelta(hours=hours)).isoformat(),stuck_for_s=hours*3600,reason='A decision or assigned run is needed.',who='thinker',kind=kind,src='fixture:1')
stuck=dict(at=now.isoformat(),error=None,count=3,stuck=[row('L-spec-7',30),row('L-spec-8',12),row(None,2,'escalation')],held=[row('L-spec-9',40,'held-ruling'),row('L-spec-10',35,'held-report')])
fixture.w('L-planner-1.jsonl',[dict(type='spec-written',subject='L-spec-7',actor='planner',ts=now.isoformat(),project='fixture-project')])
d=realm.bootstrap(24,states=[('L-spec-7','written'),('L-spec-8','written'),('L-spec-9','written')],goals=[],proving=[],now=now)
glance=dict(at=now.isoformat(),error=None,groups=[dict(text='Fixture priority.',detail='Fixture priority.')]*5,collapsed='3 specs are stuck',attention='3 specs are stuck.',needs_you=[],check_issues=[],stuck=stuck)
d.update(live=True,lane=[],glance=glance)
class Handler(realm.Handler):
 def do_GET(self):
  path=urllib.parse.urlparse(self.path).path
  if path=='/api/bootstrap': self._json(d);return
  if path=='/api/glance': self._json(glance);return
  if path=='/api/since': self._json({'seq':0,'ledger':[],'claims':[]});return
  if path.startswith('/api/'): self._json({});return
  super().do_GET()
httpd=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
print(httpd.server_address[1],flush=True)
httpd.serve_forever()
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
 const name=info.title.slice(0,3);
 await page.screenshot({path:join(evidence,name+'.png'),fullPage:true});
 await page.context().tracing.stop({path:join(evidence,name+'.trace.zip')});
});
async function open(page,width=1440,height=900){
 await page.setViewportSize({width,height});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base);await expect(page.locator('#status')).toBeHidden();
 await expect(page.locator('#h-stuck b')).toHaveText('3');
 expect(errors).toEqual([]);
 const response=await page.request.get(base+'/api/glance');const body=await response.text();
 writeFileSync(join(evidence,'glance-response.json'),JSON.stringify({url:response.url(),status:response.status(),body_sha256:createHash('sha256').update(body).digest('hex'),body_excerpt:body},null,2));
}
async function mutate(page,change){await page.evaluate(change);await page.evaluate(()=>{computeBlockers();renderGlance();mRender();});}
test('AC7 stuck tile and list at 1440x900',async({page})=>{
 for(const width of [1440,701]){
  await open(page,width, width===1440?900:800);
  const tile=page.locator('#strip #h-stuck');await expect(tile).toBeVisible();
  const b=await tile.boundingBox();expect(b.x).toBeGreaterThanOrEqual(0);expect(b.x+b.width).toBeLessThanOrEqual(width);
  await tile.click();
  const rows=page.locator('#p-body .stuck-row');await expect(rows).toHaveCount(5);
  for(const [i,id,hours] of [[0,'L-spec-7','30h'],[1,'L-spec-8','12h'],[2,'access-question','2h']]){
   await expect(rows.nth(i)).toContainText(id);await expect(rows.nth(i)).toContainText(hours);
   await expect(rows.nth(i)).toContainText('Title');await expect(rows.nth(i)).toContainText('fixture-project');
   await expect(rows.nth(i)).toContainText('A decision or assigned run is needed.');await expect(rows.nth(i)).toContainText('thinker');
  }
  await expect(page.locator('#p-body h3')).toHaveText('Held on purpose');
  expect(await page.locator('#p-body').evaluate(el=>[...el.children].findIndex(e=>e.tagName==='H3'))).toBeGreaterThan(3);
  await expect(page.locator('#glance-sentence')).toContainText('3 specs are stuck');
  await page.screenshot({path:join(evidence,'list-'+width+'.png'),fullPage:true});
  await rows.first().locator('a').click();await expect(page.locator('#p-body h2')).toContainText('L-spec-7');
 }
 await mutate(page,()=>{DATA.glance.stuck={...DATA.glance.stuck,error:'unreadable',count:0};});
 await expect(page.locator('#h-stuck b')).toHaveText('stuck list unavailable');
 await page.locator('#h-stuck').click();await expect(page.locator('#p-body')).toContainText('stuck list unavailable');
 expect(await page.locator('#h-stuck b').textContent()).not.toBe('0');
 await mutate(page,()=>{DATA.glance.stuck={...DATA.glance.stuck,error:null,count:0,stuck:[],held:[]};});
 await expect(page.locator('#h-stuck b')).toHaveText('0');await page.locator('#h-stuck').click();await expect(page.locator('#p-body')).toContainText('Nothing stuck');
 await open(page,390,844);await page.locator('#mnav [data-t="now"]').click();
 await expect(page.locator('#mview .m-tile[data-cell="h-stuck"] b')).toHaveText('3');
});
test('AC8 stuck specs are marked on the map',async({page})=>{
 for(const [width,height] of [[1440,900],[1100,700]]){
  await open(page,width,height);
  const marks=await page.evaluate(async()=>{
   await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
   return S.blockers.map(b=>({spec:b.s,hit:blockerAt({x:b.bx+b.size/2,y:b.by+b.size/2})?.s,size:b.size*MAP_SCALE(),x:ox+(b.bx+b.size/2)*scale,y:oy+(b.by+b.size/2)*scale}));
  });
  expect(marks.map(b=>b.spec)).toEqual(['L-spec-7','L-spec-8']);
  for(const mark of marks){expect(mark.hit).toBe(mark.spec);expect(mark.size).toBeGreaterThanOrEqual(12);}
  const map=await page.locator('#map').boundingBox();
  await page.mouse.click(map.x+marks[0].x,map.y+marks[0].y);
  await expect(page.locator('#p-body h2')).toContainText('L-spec-7');
  await page.locator('#p-close').click();
 }
 await page.route('**/api/glance',r=>r.fulfill({json:JSON.parse(JSON.stringify({}))}));
 const replacement=await page.evaluate(()=>({...DATA.glance,stuck:{...DATA.glance.stuck,count:2,stuck:DATA.glance.stuck.stuck.slice(1)}}));
 await page.unroute('**/api/glance');
 await page.route('**/api/glance',r=>r.fulfill({json:replacement}));
 await page.evaluate(()=>refreshGlance());
 expect(await page.evaluate(()=>S.blockers.map(b=>b.s))).toEqual(['L-spec-8']);
 await expect(page.locator('#h-stuck b')).toHaveText('2');
});
test('AC9 ledger text is escaped in the stuck list',async({page})=>{
 await open(page);
 const evil=['<img src=x onerror="window.__x=1">','"><script>window.__x=1</script>','&amp;'];
 await mutate(page,()=>{const row=DATA.glance.stuck.stuck[2];row.title='<img src=x onerror="window.__x=1">';row.reason='"><script>window.__x=1</script>';row.project='&amp;';row.subject='<img src=x onerror="window.__x=1">';});
 await page.locator('#h-stuck').click();
 expect(await page.evaluate(()=>window.__x)).toBeUndefined();await expect(page.locator('#p-body img,#p-body script')).toHaveCount(0);
 for(const text of evil)await expect(page.locator('#p-body')).toContainText(text);
});
