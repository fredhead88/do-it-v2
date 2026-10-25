// NODE_PATH=<existing dashboard node_modules> node <playwright>/cli.js test e2e/realm-glance.spec.ts --workers=1
const {test,expect}=require('playwright/test');
// Fixture boot and screenshots share the test budget on a busy build box.
test.setTimeout(120_000);
const {spawn,execFileSync}=require('node:child_process');
const {join,resolve}=require('node:path');
const {mkdirSync,mkdtempSync,writeFileSync}=require('node:fs');
const {tmpdir}=require('node:os');
const root=resolve(__dirname,'..');
const evidence=process.env.GLANCE_EVIDENCE_DIR||mkdtempSync(join(tmpdir(),'realm-glance-'));
mkdirSync(evidence,{recursive:true});
const python=String.raw`
import sys,json,pathlib,time,http.server,urllib.parse,datetime as dt,os
sys.path.insert(0,'src')
import test_realm as fixture
import realm,realm_glance as g
realm.sys_signals=lambda: {}
realm.maker_index=lambda *a: {}
realm.codex_payload=lambda *a: {'sessions':[], 'quota':{}}
realm.glance_payload=lambda *a: {}
now=dt.datetime.now(dt.timezone.utc)
def ev(kind,sid,**kw):
 return dict(type=kind,subject=sid,actor='executor',ts=(now-dt.timedelta(minutes=5)).isoformat(),**kw)
groups=g.load_priorities('realm_priorities.json')
rows=[ev('shipped',s,sha='abc') for s in groups['Listings']]
rows += [ev('window-opened','solo-1430',specs=['L-spec-1430'])]
rows += [dict(ev('decision','lane-stale:L-spec-1417',why='Held on purpose: migration order after 1418'),actor='thinker')]
states=[('L-spec-1454','building'),('L-spec-1471','graded')]
classified=[dict(spec=s,bucket='held') for s in ['L-spec-1418','L-spec-1423']]
glance=g.shape(rows,states,[],classified,groups,now)
# Include the build on the fixture map; this is never written to the live root.
fixture.w('L-builder-77.jsonl',[dict(ts=(now-dt.timedelta(minutes=4)).isoformat(),type='build-started',subject='L-spec-1454',spawn='L-builder-77')])
d=realm.bootstrap(24,states=states,goals=[],proving=[],now=now)
d['glance']=glance
d['live']=True
d['lane']=[]
class Handler(realm.Handler):
 def do_GET(self):
  path=urllib.parse.urlparse(self.path).path
  if path=='/api/bootstrap':self._json(d);return
  if path=='/api/glance':self._json(glance);return
  if path=='/baseline':
   body=pathlib.Path(sys.argv[1]).read_bytes().replace(b'/*__CODEX_JS__*/',pathlib.Path('src/realm_codex.js').read_bytes());self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
  if path=='/fixture/live' and os.environ.get('GLANCE_LIVE_FILE'):
   self._json(json.loads(pathlib.Path(os.environ['GLANCE_LIVE_FILE']).read_text()));return
  if path=='/api/since':self._json({'seq':0,'ledger':[],'claims':[]});return
  if path.startswith('/api/'):self._json({});return
  super().do_GET()
httpd=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
print(httpd.server_address[1],flush=True)
httpd.serve_forever()
`;
let server,base;
test.beforeAll(async()=>{
 const baseline=join(evidence,'baseline.html');
 writeFileSync(baseline,execFileSync('git',['show','e989558:src/realm.html'],{cwd:root}));
 server=spawn('/usr/bin/python3',['-u','-c',python,baseline],{cwd:root,stdio:['ignore','pipe','pipe']});
 base=await new Promise((resolve,reject)=>{let out='';server.stdout.on('data',chunk=>{out+=chunk;const match=/^(\d+)\n/.exec(out);if(match)resolve('http://127.0.0.1:'+match[1]);});server.on('exit',c=>reject(Error('fixture server exit '+c)));server.stderr.on('data',c=>process.stderr.write(c));});
});
test.afterAll(()=>server?.kill());
async function open(page,path='/'){
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',route=>new URL(route.request().url()).origin===base?route.continue():route.abort());
 await page.goto(base+path);await expect(page.locator('#status')).toBeHidden();
 if(page.viewportSize().width<=700)await page.locator('#mnav [data-t="map"]').click();
 expect(errors).toEqual([]);
 return errors;
}
async function geometry(page){return page.evaluate(()=>{
 const box=s=>{const r=document.querySelector(s).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};};
 return {header:box('.hud'),stage:box('.stage'),map:box('#map'),view:box('.view'),overflow:document.documentElement.scrollWidth>innerWidth};
});}
for(const width of [1440,390,320])test('one line, six sentences, storage and unchanged map at '+width,async({page})=>{
 await page.setViewportSize({width,height:width>700?1000:844});
 const errors=await open(page,'/baseline');const baseline=await geometry(page);
 await page.goto(base);await expect(page.locator('#status')).toBeHidden();
 if(width<=700)await page.locator('#mnav [data-t="map"]').click();
 const before=await geometry(page);expect(before).toEqual(baseline);
 const toggle=page.locator('#glance-toggle');
 await expect(toggle).toHaveAttribute('aria-expanded','false');
 await expect(page.locator('#glance-lines')).toBeHidden();
 await expect(toggle).toContainText("Dina's tools merging now");
 expect(await page.locator('#glance-sentence a').count()).toBe(0);
 await page.screenshot({path:join(evidence,'collapsed-'+width+'.png'),fullPage:true});
 await toggle.click();await expect(toggle).toHaveAttribute('aria-expanded','true');
 await expect(page.locator('#glance-lines p')).toHaveCount(6);
 await expect(page.locator('#glance-lines')).toContainText('held on purpose');
 await expect(page.locator('#glance-lines')).toContainText('Ads re-pull before Nov 1');
 expect(await geometry(page)).toEqual(before);
 const rows=await page.locator('#glance-lines p').evaluateAll(els=>els.map(e=>({text:e.textContent,height:e.getBoundingClientRect().height,line:parseFloat(getComputedStyle(e).lineHeight),padding:parseFloat(getComputedStyle(e).paddingTop)+parseFloat(getComputedStyle(e).paddingBottom)})));
 expect(rows.every(r=>r.height<=r.line+r.padding+1),JSON.stringify(rows)).toBeTruthy();
 await page.screenshot({path:join(evidence,'expanded-'+width+'.png'),fullPage:true});
 await page.reload();await expect(toggle).toHaveAttribute('aria-expanded','true');
 await toggle.focus();await page.keyboard.press('Enter');await expect(toggle).toHaveAttribute('aria-expanded','false');
 await page.reload();await expect(toggle).toHaveAttribute('aria-expanded','false');
 // Snapshot refreshes replace the sentence, and replay never presents current facts as historical.
 await page.evaluate(()=>{applySnapshot({...DATA,glance:{...DATA.glance,collapsed:'Refreshed priority sentence'}});renderGlance();});
 await expect(toggle).toContainText('Refreshed priority sentence');
 await page.evaluate(()=>{DATA.glance={...DATA.glance,attention:'1 thing needs you.',needs_you:[{spec:'L-spec-1454',ask:'Approve access for the GitHub fix'}]};renderGlance();});
 await toggle.click();await page.locator('#glance-lines button').click();
 await expect(page.locator('#p-body')).toContainText('Approve access for the GitHub fix');
 await page.evaluate(()=>{LIVE=false;REMOTE=false;renderGlance();});
 await expect(toggle).toContainText('available in live mode');
 await page.evaluate(()=>{LIVE=true;DATA.glance.at='2020-01-01T00:00:00Z';renderGlance();});
 await expect(toggle).toContainText('fresh update');
 await page.evaluate(()=>{DATA.glance={error:'What matters is unavailable'};renderGlance();});
 await expect(toggle).toContainText('unavailable');
 writeFileSync(join(evidence,'geometry-'+width+'.json'),JSON.stringify({baseline,collapsed:before,expanded:before,rows,errors},null,2));
 expect(errors).toEqual([]);
});
test('storage exceptions, phone NOW view and live priority screenshot',async({page})=>{
 await page.setViewportSize({width:390,height:844});
 await page.addInitScript(()=>{Object.defineProperty(Storage.prototype,'getItem',{value(){throw Error('storage denied');}});Object.defineProperty(Storage.prototype,'setItem',{value(){throw Error('storage full');}});});
 const errors=await open(page);
 await page.locator('#mnav [data-t="now"]').click();
 await page.locator('#glance-toggle').click();await expect(page.locator('#glance-lines')).toBeVisible();
 expect((await geometry(page)).overflow).toBe(false);
 await page.screenshot({path:join(evidence,'phone-now.png'),fullPage:true});
 if(process.env.GLANCE_LIVE_FILE){
  const live=await (await page.request.get(base+'/fixture/live')).json();
  await page.evaluate(d=>{DATA.glance=d;renderGlance();},live);
  await page.screenshot({path:join(evidence,'live-priorities-phone.png'),fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
  await page.screenshot({path:join(evidence,'live-priorities-desktop.png'),fullPage:true});
 }
 expect(errors).toEqual([]);
});
