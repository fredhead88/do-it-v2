// Real local Handler + scorecard/pace fixtures; no production reads.
const {test,expect}=require('playwright/test');
const {spawn}=require('node:child_process');
const {mkdtempSync}=require('node:fs');
const {join,resolve}=require('node:path');
const {tmpdir}=require('node:os');
const root=resolve(__dirname,'..');
const evidence=process.env.SCORECARD_EVIDENCE_DIR||mkdtempSync(join(tmpdir(),'realm-scorecard-evidence-'));
console.log('Scorecard evidence: '+evidence);
test.setTimeout(120_000);
const python=String.raw`
import sys,datetime as dt,http.server,urllib.parse,json,contextlib,signal
def stop(*args): raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
sys.path.insert(0,'src')
import test_realm_scorecard as fixture
import realm
with fixture.scratch() as (root,now):
 # A retained SD6 sample can also carry the sampler's stall bucket fields.
 path=root/'state/pace-samples.jsonl'
 rows=[json.loads(line) for line in path.read_text().splitlines()]
 for i,row in enumerate(rows):
  if i%2==0 and row['ts'][:10]==str((now-dt.timedelta(days=1)).date()):
   row.update(ok=True,buckets={'STUCK':['L-spec-9']},builder_capacity=1)
 path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
 realm.sys_signals=lambda: {}
 realm.maker_index=lambda *a: {}
 realm.glance_payload=lambda *a: {}
 data=realm.bootstrap(24,states=[],goals=[],proving=[],now=now)
 data.update(live=True,lane=[],glance={'at':now.isoformat(),'collapsed':'Fixture scorecard','attention':'','needs_you':[],'stuck':{'count':0,'stuck':[],'held':[],'error':None}})
 class Handler(realm.Handler):
  def do_GET(self):
   u=urllib.parse.urlparse(self.path)
   if u.path=='/api/bootstrap': self._json(data);return
   if u.path=='/api/glance': self._json(data['glance']);return
   if u.path=='/api/since': self._json({'seq':0,'ledger':[],'claims':[]});return
   if u.path=='/api/scorecard' and urllib.parse.parse_qs(u.query).get('unsafe')==['1']:
    original=realm.scorecard.day_rows
    attack='<img src=x onerror="window.__xss=1"><b>x</b>'
    def deep(v):
     if isinstance(v,str): return v+attack
     if isinstance(v,list): return [deep(x) for x in v]
     if isinstance(v,dict): return {k:deep(x) for k,x in v.items()}
     return v
    def wrapped(*args,**kwargs):
     days=original(*args,**kwargs)
     for row in days:
      if not row.get('top'): row['top']=[dict(spec='L-spec-1',kinds=['stalled'],reason='fixture')]
     return [deep(row) for row in days]
    from unittest.mock import patch
    realm._SCORECARD_CACHE=None
    with patch.object(realm.scorecard,'day_rows',side_effect=wrapped): self._json(realm.scorecard_payload())
    realm._SCORECARD_CACHE=None
    return
   if u.path.startswith('/api/') and u.path!='/api/scorecard': self._json({});return
   super().do_GET()
 httpd=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
 print(httpd.server_port,flush=True)
 httpd.serve_forever()
`;
let server,base;
test.beforeAll(async()=>{
 server=spawn('/usr/bin/python3',['-u','-c',python],{cwd:root,stdio:['ignore','pipe','pipe']});
 base=await new Promise((done,fail)=>{let out='';server.stdout.on('data',c=>{out+=c;const m=/^(\d+)\n/.exec(out);if(m)done('http://127.0.0.1:'+m[1]);});server.on('exit',c=>fail(Error('fixture exit '+c)));server.stderr.on('data',c=>process.stderr.write(c));});
});
test.afterAll(()=>server?.kill());
test.beforeEach(async({page})=>{
 await page.context().tracing.start({screenshots:false,snapshots:false});
 await page.emulateMedia({reducedMotion:'reduce'});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
});
test.afterEach(async({page},info)=>{
 const name=info.title.replace(/[^a-zA-Z0-9]+/g,'-');
 await page.screenshot({path:join(evidence,name+'.png'),fullPage:true});
 await page.context().tracing.stop({path:join(evidence,name+'.trace.zip')});
});
async function open(page,width=1440){
 await page.setViewportSize({width,height:900});
 await page.goto(base);
 await expect(page.locator('#status')).toBeHidden();
 await expect(page.locator('#b-scorecard')).toBeEnabled();
 // Keep the painted map still while exercising the independent scorecard.
 await page.evaluate(()=>{const schedule=window.requestAnimationFrame.bind(window);window.requestAnimationFrame=callback=>callback===loop?0:schedule(callback);});
}
async function panel(page){
 const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/scorecard');
 await page.locator('#b-scorecard').click();
 const data=await (await response).json();
 await expect(page.locator('.scorecard-day')).toHaveCount(7);
 return data;
}
for(const width of [1440,400]){
 test(`AC5 scorecard renders seven days, three groups, banded cells ${width}`,async({page})=>{
  await open(page,width);const data=await panel(page);
  expect(await page.locator('.scorecard-day').evaluateAll(es=>es.map(e=>e.dataset.date))).toEqual(data.days.map(r=>r.date));
  expect(data.days.some(r=>r.capacity_unused===true)).toBe(true);
  expect(data.days.some(r=>r.sampled.pace===false)).toBe(true);
  const columns=await page.locator('.scorecard-day').evaluateAll(es=>es.map(e=>({
   date:e.dataset.date,heading:e.querySelector('h3').textContent,
   groups:[...e.querySelectorAll('.scorecard-group h4')].map(x=>x.textContent),
   unused:!!e.querySelector('.capacity-unused'),
   cells:[...e.querySelectorAll('.scorecard-cell')].map(c=>({key:c.dataset.key,value:c.querySelector('.value').textContent,band:c.querySelector('.band').textContent,classes:[...c.classList]}))
  })));
  for(const [i,row] of data.days.entries()){
   const column=columns[i];
   expect(column.heading).toBe(row.date+(row.date===data.generated_at.slice(0,10)?' · partial':''));
   expect(column.groups).toEqual(['Output','Capacity used','Loss']);expect(column.unused).toBe(row.capacity_unused===true);
   expect(column.cells.length).toBe(row.cells.length);
   for(const cell of row.cells){const node=column.cells.find(c=>c.key===cell.key);
    expect(node.value).toBe(cell.text);expect(node.band).toBe(cell.band);
    expect(node.classes).toContain(['green','amber','red'].includes(cell.band)?cell.band:'neutral');
    if(cell.band==='not sampled'||cell.value===null){expect(node.classes).not.toContain('green');expect(node.value).not.toBe('0');}
   }
  }
 });
 test(`AC7 pace view draws six series with ceiling, pace line and reset marker ${width}`,async({page})=>{
  await open(page,width);const data=await panel(page);
  const series=['disk_used','ram_used','swap_used','claude_5h','claude_7d','codex_7d'];
  await expect(page.locator('.scorecard-chart')).toHaveCount(6);
  for(const key of series){
   const chart=page.locator(`.scorecard-chart[data-series="${key}"]`);
   await expect(chart.locator('.capacity')).toHaveAttribute('data-value','100');
   if(['disk_used','ram_used'].includes(key))await expect(chart.locator('.ceiling')).toHaveAttribute('data-value',String(data.ceiling[key==='disk_used'?'disk':'ram']));
   const latest=[...data.pace].reverse().find(r=>r[key+'_pct']!==null&&r[key+'_pct']!==undefined);
   await expect(chart.locator('.newest')).toHaveText(latest[key+'_pct']+'% · '+latest.ts);
   const expectedSegments=[];let segment=[];let previous=null;
   for(const row of data.pace){const t=Date.parse(row.ts),v=row[key+'_pct'];if(previous!==null&&t-previous>6*3600000){if(segment.length)expectedSegments.push(segment);segment=[];}if(v===null||v===undefined){if(segment.length)expectedSegments.push(segment);segment=[];}else segment.push(v);previous=t;}
   if(segment.length)expectedSegments.push(segment);
   await expect(chart.locator('.reading')).toHaveCount(expectedSegments.length);
   expect(await chart.locator('.gap').count()).toBeGreaterThan(0);
   await expect(chart.locator('.gap').first()).toContainText('not sampled');
   if(key.startsWith('claude')||key.startsWith('codex')){
    expect(await chart.locator('.pace-line').count()).toBeGreaterThan(0);
    const resets=[...new Set(data.pace.map(r=>Date.parse(r[key+'_reset'])).filter(t=>t>=Date.parse(data.generated_at)-7*86400000&&t<=Date.parse(data.generated_at)))];
    expect(resets.length).toBeGreaterThan(0);await expect(chart.locator('.reset')).toHaveCount(resets.length);
   }
  }
 });
 test(`AC8 scorecard is lazy, closed by default and does not move the main view ${width}`,async({page})=>{
  let requests=0;page.on('request',r=>{if(new URL(r.url()).pathname==='/api/scorecard')requests++;});
  await open(page,width);
  const viewport=page.viewportSize();
  await expect(page.locator('#scorecard')).toBeHidden();expect(requests).toBe(0);
  await expect(page.locator('#strip-primary')).toBeVisible();await expect(page.locator('#strip-primary #b-scorecard')).toBeVisible();
  const button=await page.locator('#b-scorecard').boundingBox();
  expect(button.x).toBeGreaterThanOrEqual(0);expect(button.y).toBeGreaterThanOrEqual(0);
  expect(button.x+button.width).toBeLessThanOrEqual(viewport.width);expect(button.y+button.height).toBeLessThanOrEqual(viewport.height);
  const strip=await page.locator('#strip').boundingBox();expect(strip.height).toBeLessThanOrEqual(56);
  const near=(a,b)=>{for(const k of ['x','y','width','height'])expect(Math.abs(a[k]-b[k])).toBeLessThanOrEqual(1);};
  const stage=await page.locator('.stage').boundingBox();
  await page.locator('#b-scorecard').focus();await page.keyboard.press('Enter');
  await expect(page.locator('.scorecard-day')).toHaveCount(7);expect(requests).toBe(1);
  near(await page.locator('.stage').boundingBox(),stage);
  if(width===400){
   const fit=await page.evaluate(()=>({sw:document.documentElement.scrollWidth,cw:document.documentElement.clientWidth,bsw:document.body.scrollWidth,bcw:document.body.clientWidth,inner:(()=>{const d=document.querySelector('.scorecard-days');return {sw:d.scrollWidth,cw:d.clientWidth,overflow:getComputedStyle(d).overflowX};})()}));
   expect(fit.sw).toBeLessThanOrEqual(fit.cw);expect(fit.bsw).toBeLessThanOrEqual(fit.bcw);
   expect(fit.inner.overflow).toBe('auto');expect(fit.inner.sw).toBeGreaterThan(fit.inner.cw);
  }
  await page.locator('#scorecard-close').click();await expect(page.locator('#scorecard')).toBeHidden();
  near(await page.locator('.stage').boundingBox(),stage);
  expect(requests).toBe(1);
  await page.locator('.spd').first().evaluate(e=>e.click());await expect(page.locator('#b-scorecard')).toBeEnabled();
  await page.locator('#scrub').evaluate(e=>{e.value='200';e.dispatchEvent(new Event('change',{bubbles:true}));});
  await expect(page.locator('#b-scorecard')).toBeEnabled();expect(requests).toBe(1);
  await page.locator('#b-scorecard').click();await expect.poll(()=>requests).toBe(2);
  await expect(page.locator('.scorecard-day')).toHaveCount(7);
  // Open preference survives reload; the restored fetch starts after the first useful paint.
  await page.reload();await expect(page.locator('#scorecard')).toBeVisible();await expect(page.locator('.scorecard-day')).toHaveCount(7);
  const timing=await page.evaluate(()=>({paint:performance.getEntriesByType('paint').map(e=>({name:e.name,at:e.startTime})),fetchAt:performance.getEntriesByType('resource').filter(e=>new URL(e.name).pathname==='/api/scorecard').map(e=>e.startTime)}));
  const firstPaint=Math.min(...timing.paint.map(e=>e.at));
  expect(timing.paint.length).toBeGreaterThan(0);expect(timing.fetchAt.length).toBe(1);expect(timing.fetchAt[0]).toBeGreaterThanOrEqual(firstPaint);
  // Availability cases: disabled ones say so and make no request.
  const before=requests;
  await page.evaluate(()=>{DATA.live=false;scorecardCapability();});
  await expect(page.locator('#b-scorecard')).toBeDisabled();await expect(page.locator('#b-scorecard')).toHaveAttribute('title','live Realm only');await expect(page.locator('#scorecard')).toBeHidden();
  await page.locator('#b-scorecard').click({force:true,timeout:2000}).catch(()=>{});await page.evaluate(()=>setScorecard(true));await page.waitForTimeout(150);
  expect(requests).toBe(before);await expect(page.locator('#scorecard')).toBeHidden();
  await page.evaluate(()=>{DATA.live=false;REMOTE=true;LIVE=true;scorecardCapability();});
  await expect(page.locator('#b-scorecard')).toBeDisabled();await expect(page.locator('#b-scorecard')).toHaveAttribute('title','live Realm only');
  await page.locator('#b-scorecard').click({force:true,timeout:2000}).catch(()=>{});await page.evaluate(()=>setScorecard(true));await page.waitForTimeout(150);
  expect(requests).toBe(before);await expect(page.locator('#scorecard')).toBeHidden();
  await page.evaluate(()=>{DATA.live=true;REMOTE=true;scorecardCapability();});
  await expect(page.locator('#b-scorecard')).toBeDisabled();expect(requests).toBe(before);
 });
 test(`AC8 throwing localStorage does not break load or opening ${width}`,async({page})=>{
  let requests=0;page.on('request',r=>{if(new URL(r.url()).pathname==='/api/scorecard')requests++;});
  await page.addInitScript(()=>{for(const k of ['getItem','setItem','removeItem'])Object.defineProperty(Storage.prototype,k,{configurable:true,value(){throw new DOMException('blocked','SecurityError');}});});
  await open(page,width);
  await expect(page.locator('#scorecard')).toBeHidden();expect(requests).toBe(0);
  await page.locator('#b-scorecard').click();
  await expect(page.locator('.scorecard-day')).toHaveCount(7);expect(requests).toBe(1);
 });
}
test('AC6 copy button puts exactly the server text on the clipboard',async({page,context})=>{
 await context.grantPermissions(['clipboard-read','clipboard-write']);
 await open(page);const data=await panel(page);
 for(const row of data.days){
  const column=page.locator(`.scorecard-day[data-date="${row.date}"]`);
  console.log(row.date+' compared metric cells: '+row.cells.map(c=>c.key).join(','));
  for(const cell of row.cells){
   if(typeof cell.value==='number'||(cell.value&&typeof cell.value==='object'))for(const token of cell.text.split('; reset ')[0].match(/\d+(\.\d+)?/g)||[])expect(row.copy_text).toContain(token);
   expect(row.copy_text).toContain('['+cell.band+']');
  }
  await column.getByRole('button',{name:'Copy '+row.date}).click();
  expect(await page.evaluate(()=>navigator.clipboard.readText())).toBe(row.copy_text);
  expect(await page.evaluate(async()=>(await navigator.clipboard.read()).flatMap(x=>x.types))).toEqual(['text/plain']);
 }
 await page.evaluate(()=>{navigator.clipboard.writeText=()=>Promise.reject(Error('fixture denied'));});
 await page.locator('.scorecard-day button').first().click();await expect(page.locator('.copy-status').first()).toHaveText('copy failed');
});
test('AC9 payload strings render as text, never as markup',async({page,context})=>{
 await context.grantPermissions(['clipboard-read','clipboard-write']);
 const suffix='<img src=x onerror="window.__xss=1"><b>x</b>';
 await open(page);
 await page.route('**/api/scorecard',async route=>{
  const response=await page.request.get(base+'/api/scorecard?unsafe=1');
  await route.fulfill({response});
 });
 const data=await panel(page);
 // The route really returned wrapped strings, deep: ids, bands, labels, text, reasons and copy_text.
 const row=data.days[0];
 expect(row.copy_text.endsWith(suffix)).toBe(true);expect(row.date.endsWith(suffix)).toBe(true);expect(row.cells[0].key.endsWith(suffix)).toBe(true);
 expect(row.cells[0].label.endsWith(suffix)).toBe(true);expect(row.cells[0].text.endsWith(suffix)).toBe(true);expect(row.cells[0].band.endsWith(suffix)).toBe(true);
 expect(row.top[0].spec.endsWith(suffix)).toBe(true);expect(row.top[0].reason.endsWith(suffix)).toBe(true);
 expect(await page.evaluate(()=>window.__xss)).toBeUndefined();
 await expect(page.locator('#scorecard img')).toHaveCount(0);
 await expect(page.locator('#scorecard b')).toHaveCount(0);
 await expect(page.locator('#scorecard')).toContainText(suffix);
 await expect(page.locator('.scorecard-reason').first()).toContainText(suffix);
 await page.locator('.scorecard-day').first().getByRole('button').first().click();
 expect(await page.evaluate(()=>navigator.clipboard.readText())).toBe(row.copy_text);
 await page.waitForTimeout(150);
 expect(await page.evaluate(()=>window.__xss)).toBeUndefined();
 await page.route('**/api/scorecard',route=>route.fulfill({json:{error:'scorecard unavailable (RuntimeError)',generated_at:new Date().toISOString()}}));
 await page.locator('#scorecard-refresh').click();
 await expect(page.locator('#scorecard-status')).toHaveText('scorecard unavailable (RuntimeError)');
});
