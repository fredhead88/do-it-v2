// Uses the existing Playwright installation and an isolated test_realm root.
const {test,expect}=require('playwright/test');
const {spawn,execFileSync}=require('node:child_process');
const {mkdtempSync,writeFileSync}=require('node:fs');
const {join,resolve}=require('node:path');
const {tmpdir}=require('node:os');
const root=resolve(__dirname,'..');
const evidence=process.env.LAYOUT_EVIDENCE_DIR||mkdtempSync(join(tmpdir(),'realm-layout-'));
test.setTimeout(120_000);
const python=String.raw`
import sys,pathlib,datetime as dt,http.server,urllib.parse
sys.path.insert(0,'src')
import test_realm as fixture
import realm
realm.sys_signals=lambda: {}
realm.maker_index=lambda *a: {}
realm.glance_payload=lambda *a: {}
now=dt.datetime.now(dt.timezone.utc)
start=(now-dt.timedelta(minutes=5)).isoformat()
fixture.w('L-builder-7.jsonl',[dict(ts=start,type='build-started',subject='L-spec-7',spawn='L-builder-7',backend='codex')])
realm.codex_payload=lambda *a: {'sessions':[dict(id='L-builder-7',kind='doit-build',subject='L-spec-7',role='builder',state='live',started=start,last=start)],'quota':{'weekly_used_pct':71}}
d=realm.bootstrap(24,states=[('L-spec-7','building')],goals=[],proving=[],now=now)
d['omens']={'items':[dict(id='layout-need',kind='decide',sev=1,title='A decision needs you',detail='Review fixture access',anchor='keep',subjects=['L-spec-7'],owner='operator',next='Review access',since=start,last=start)]}
d.update(live=True,lane=[],glance={'at':now.isoformat(),'collapsed':'A fixture build is moving forward.','attention':'1 thing needs you.','needs_you':[]})
class Handler(realm.Handler):
 def do_GET(self):
  path=urllib.parse.urlparse(self.path).path
  if path=='/api/bootstrap': self._json(d);return
  if path=='/api/glance': self._json(d['glance']);return
  if path=='/baseline':
   body=pathlib.Path(sys.argv[1]).read_bytes().replace(b'/*__CODEX_JS__*/',pathlib.Path('src/realm_codex.js').read_bytes())
   self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
  if path=='/api/since': self._json({'seq':0,'ledger':[],'claims':[]});return
  if path.startswith('/api/'): self._json({});return
  super().do_GET()
httpd=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
print(httpd.server_address[1],flush=True)
httpd.serve_forever()
`;
let server,base;
test.beforeAll(async()=>{
 const baseline=join(evidence,'baseline.html');
 writeFileSync(baseline,execFileSync('git',['show','5b75a38:src/realm.html'],{cwd:root}));
 server=spawn('/usr/bin/python3',['-u','-c',python,baseline],{cwd:root,stdio:['ignore','pipe','pipe']});
 base=await new Promise((done,fail)=>{let out='';server.stdout.on('data',c=>{out+=c;const m=/^(\d+)\n/.exec(out);if(m)done('http://127.0.0.1:'+m[1]);});server.on('exit',c=>fail(Error('fixture exit '+c)));server.stderr.on('data',c=>process.stderr.write(c));});
});
test.afterAll(()=>server?.kill());
test.beforeEach(async({page})=>{
 await page.context().tracing.start({screenshots:true,snapshots:true});
 await page.emulateMedia({reducedMotion:'reduce'});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
});
test.afterEach(async({page},info)=>{
 const name=info.title.split(':')[0];
 await page.screenshot({path:join(evidence,name+'.png'),fullPage:true});
 await page.context().tracing.stop({path:join(evidence,name+'.trace.zip')});
});
async function open(page,width=1440,height=900,path='/'){
 await page.setViewportSize({width,height});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+path);await expect(page.locator('#status')).toBeHidden();
 // Stable needs-you fixture, independent of incidental system-health omens.
 await page.evaluate(()=>{
  S.playing=false;S.alarmT=-1e9;
  S.alarms=[{id:'layout-need',kind:'decide',needs:true,sev:1,title:'A decision needs you',detail:'Review fixture access',anchor:'keep',subjects:['L-spec-7'],owner:'you',next:'Review access'}];
  renderOmens();
 });
 expect(errors).toEqual([]);
}
async function state(page){return page.evaluate(()=>{
 const box=s=>{const r=document.querySelector(s).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};};
 return {map:box('#map'),strip:box('#strip'),scale:MAP_SCALE(),width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,scrollHeight:document.documentElement.scrollHeight};
});}
function sameMap(a,b){expect(b.map).toEqual(a.map);expect(b.scale).toBe(a.scale);}
async function inside(page,selector,parent='#strip'){
 await expect(page.locator(selector)).toBeVisible();
 const boxes=await page.evaluate(({selector,parent})=>{
  const r=document.querySelector(selector).getBoundingClientRect(),p=document.querySelector(parent).getBoundingClientRect();
  return {fits:r.width>0&&r.height>0&&r.x>=p.x&&r.y>=p.y&&r.right<=p.right+.1&&r.bottom<=p.bottom+.1};
 },{selector,parent});expect(boxes.fits,selector+' inside '+parent).toBe(true);
}
function fit(s,ratio=true){
 expect(s.map.x).toBeCloseTo(0,0);expect(s.map.y).toBeCloseTo(s.strip.height,0);
 expect(s.map.width).toBeCloseTo(s.width,0);expect(s.map.height).toBeCloseTo(s.height-s.strip.height,0);
 expect(Math.abs(s.scale-Math.min(s.width/1280,(s.height-s.strip.height)/800))).toBeLessThan(.005);
 if(ratio)expect(1280*800*s.scale*s.scale/(s.width*s.height)).toBeGreaterThanOrEqual(.70);
 expect(s.scrollWidth).toBeLessThanOrEqual(s.width);expect(s.scrollHeight).toBeLessThanOrEqual(s.height);
}
async function closed(page){
 await expect(page.locator('#app')).toHaveClass(/nopanel/);
 for(const id of ['#panel','#omens','#log'])await expect(page.locator(id)).toBeHidden();
}
const menuIds=['h-clock','h-quota','h-owed','h-accept','h-close','h-proving','h-flight','h-merge','h-shelf','h-ship','h-fail','h-disk','b-live','b-play','b-charters','b-yitzy','b-clock','b-census','b-tour','b-help','scrub','b-chronicle'];
test('AC1: compact strip and complete overlay menu',async({page})=>{
 await open(page);const before=await state(page);
 expect(await page.locator('#strip').evaluate(e=>e.tagName)).toBe('HEADER');await expect(page.locator('#strip')).toHaveClass(/hud/);
 expect(before.strip).toMatchObject({x:0,y:0,width:1440});expect(before.strip.height).toBeLessThanOrEqual(56);
 for(const id of ['mode','glance-toggle','h-exec','h-stuck','h-esc','h-pipe','b-omens','b-menu'])await inside(page,'#'+id);
 await expect(page.locator('#glance-sentence')).not.toBeEmpty();
 for(const id of menuIds)await expect(page.locator('#'+id)).toBeHidden();
 await page.locator('#b-menu').click();
 for(const id of menuIds)await inside(page,'#'+id,'#strip-menu');
 sameMap(before,await state(page));
 await page.setViewportSize({width:390,height:844});
 for(const id of menuIds)await expect(page.locator('#'+id)).toHaveCount(1);
});
test('AC2: stuck stays unclipped at every desktop width and glance overlays',async({page})=>{
 for(const width of [1440,1100,900,701]){
  await open(page,width,800);const before=await state(page);expect(before.strip.height).toBeLessThanOrEqual(56);
  await inside(page,'#h-stuck');await inside(page,'#glance-toggle');expect(before.scrollWidth).toBeLessThanOrEqual(width);
  expect(await page.locator('#h-stuck').evaluate(e=>{
   const r=e.getBoundingClientRect();let p=e.parentElement;
   while(p){const b=p.getBoundingClientRect();if(r.x<b.x||r.right>b.right+.1||r.y<b.y||r.bottom>b.bottom+.1)return false;if(p.id==='strip')return true;p=p.parentElement;}return false;
  })).toBe(true);
  await page.locator('#glance-toggle').click();await expect(page.locator('#glance-lines')).toBeVisible();sameMap(before,await state(page));
  await page.locator('#glance-toggle').click();
 }
});
test('AC3: map fills the desktop viewport and paints at least seventy percent',async({page})=>{
 for(const [width,height] of [[1440,900],[1920,1080]]){await open(page,width,height);fit(await state(page));}
});
test('AC4: resizing repaints the map within one second',async({page})=>{
 await open(page);
 // Measure the browser's resize-to-paint time; protocol round trips on the
 // shared box can themselves exceed one second even when the fit is ready.
 await page.evaluate(()=>addEventListener('resize',()=>{
  const start=performance.now();requestAnimationFrame(()=>{
   const strip=document.querySelector('#strip').getBoundingClientRect();
   window.layoutResizeTiming={width:innerWidth,height:innerHeight,elapsed:performance.now()-start,scale:MAP_SCALE(),strip:strip.height};
  });
 }));
 const timings=[];
 const hash=()=>page.evaluate(()=>{const data=cv.getContext('2d').getImageData(0,0,cv.width,cv.height).data;let h=2166136261;for(let i=0;i<data.length;i+=7)h=Math.imul(h^data[i],16777619);return h;});
 let previous;
 for(const [width,height] of [[1440,900],[1100,700],[1280,720],[1920,1080]]){
  await page.setViewportSize({width,height});
  if(previous!==undefined){
   await page.waitForFunction(({width,height})=>window.layoutResizeTiming?.width===width&&window.layoutResizeTiming?.height===height,{width,height});
   const timing=await page.evaluate(()=>window.layoutResizeTiming);timings.push(timing);
   expect(timing.elapsed).toBeLessThanOrEqual(1000);
   expect(Math.abs(timing.scale-Math.min(width/1280,(height-timing.strip)/800))).toBeLessThan(.005);
  }
  await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
  fit(await state(page));const current=await hash();if(previous!==undefined)expect(current).not.toBe(previous);previous=current;
 }
 writeFileSync(join(evidence,'resize-timings.json'),JSON.stringify(timings,null,2));
});
test('AC5: letterboxes continue the painted world edge',async({page})=>{
 for(const [width,height] of [[2000,700],[1000,1000]]){
  await open(page,width,height);
  await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
  const differences=await page.evaluate(()=>{
   const c=cv.getContext('2d'),d=cv._dpr,left=Math.ceil(ox*d),right=Math.ceil((ox+W*scale)*d)-1,top=Math.ceil(oy*d),bottom=Math.ceil((oy+H*scale)*d)-1;
   const diff=(x,y,xx,yy)=>{const a=c.getImageData(x,y,1,1).data,b=c.getImageData(xx,yy,1,1).data;return Math.max(...Array.from(a,(n,i)=>Math.abs(n-b[i])));};
   const result=[];for(const t of [.2,.5,.8]){
    if(left>0){const y=Math.round(top+(bottom-top)*t);result.push(diff(left-1,y,left,y),diff(right+1,y,right,y));}
    if(top>0){const x=Math.round(left+(right-left)*t);result.push(diff(x,top-1,x,top),diff(x,bottom+1,x,bottom));}
   }return result;
  });expect(differences).toHaveLength(6);expect(Math.max(...differences)).toBeLessThanOrEqual(12);
 }
});
test('AC6: drawers and lists start closed irrespective of omens preference',async({page})=>{
 for(const pref of [null,'min']){
  await page.addInitScript(pref=>{localStorage.clear();if(pref)localStorage.setItem('realm.omens',pref);},pref);
  for(const width of [1440,900,701]){await open(page,width,900);await closed(page);fit(await state(page),false);}
 }
});
test('AC7: every on-demand surface opens over an unchanged map',async({page})=>{
 await open(page);await closed(page);const before=await state(page);
 const intersects=async selector=>{await expect(page.locator(selector)).toBeVisible();const b=await page.locator(selector).boundingBox(),m=before.map;expect(b.x<m.x+m.width&&b.x+b.width>m.x&&b.y<m.y+m.height&&b.y+b.height>m.y).toBe(true);sameMap(before,await state(page));};
 await page.locator('#b-menu').click();await page.locator('#b-charters').click();await intersects('#panel');
 const drawer=await page.locator('#panel').boundingBox();expect(drawer.x+drawer.width).toBe(1440);expect(drawer.width).toBeLessThanOrEqual(420);
 await page.locator('#p-close').click();await closed(page);
 await page.locator('.codex-target[data-codex="L-builder-7"]').click();await intersects('#panel');await expect(page.locator('.codex-inspector')).toBeVisible();
 await page.locator('#p-close').click();await closed(page);
 await page.locator('#b-omens').click();await intersects('#omens');await page.locator('#b-omens').click();await closed(page);
 await page.locator('#b-menu').click();await page.locator('#b-chronicle').click();await intersects('#log');
 await page.locator('#b-menu').click();await page.locator('#b-chronicle').click();await closed(page);
 await page.evaluate(()=>openPanel('<h2>x</h2>',undefined));await intersects('#panel');expect(await page.locator('#p-body').evaluate(e=>e.innerHTML)).toBe('<h2>x</h2>');await page.locator('#p-close').click();await closed(page);sameMap(before,await state(page));
});
test('AC8: closed omens retain their needs-you count in the strip',async({page})=>{
 for(const width of [1440,701]){
  await open(page,width,900);
  for(const count of [1,2,0]){
   if(count===2)await page.evaluate(()=>{S.alarms.push({...S.alarms[0],id:'layout-second'});renderOmens();});
   if(count===0)await page.evaluate(()=>{S.alarms=[];renderOmens();});
   await inside(page,'#b-omens');await expect(page.locator('#b-omens')).toHaveText(count?'NEEDS YOU · '+count:'OMENS');await expect(page.locator('#omens')).toBeHidden();
  }
 }
});
test('AC9: every map text remains at least twelve CSS pixels',async({page})=>{
 await page.addInitScript(()=>{
  window.mapText=[];const original=CanvasRenderingContext2D.prototype.fillText;
  CanvasRenderingContext2D.prototype.fillText=function(text,...args){
   if(this.canvas.id==='map'||this.canvas.id==='codex-light'||!this.canvas.isConnected){
    const px=parseFloat(/([\d.]+)px/.exec(this.font)[1]);
    const size=px*(this.canvas.isConnected?Math.hypot(this.getTransform().a,this.getTransform().b)/devicePixelRatio:(typeof MAP_SCALE==='function'?MAP_SCALE():1));
    window.mapText.push({text:String(text),px,size});
   }return original.call(this,text,...args);
  };
 });
 for(const [width,height] of [[1440,900],[1100,700],[1920,1080]]){
  await open(page,width,height);
  const data=await page.evaluate(async()=>{window.mapText=[];await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return {calls:window.mapText,places:Object.values(PLACES).filter(p=>p.kind!=='outer'&&p.kind!=='codex').map(p=>p.n.toUpperCase())};});
  expect(data.calls.length).toBeGreaterThan(0);for(const call of data.calls)expect(call.size,JSON.stringify(call)).toBeGreaterThanOrEqual(12);
  const texts=data.calls.map(c=>c.text);for(const name of [...data.places,'THE OUTER LANDS','CODEX GATE','L-spec-7'])expect(texts).toContain(name);
  writeFileSync(join(evidence,'labels-'+width+'.json'),JSON.stringify(data,null,2));
 }
});
test('AC10: phone MAP and NOW geometry matches the original page',async({page})=>{
 const geometry=()=>page.evaluate(()=>{const b=s=>{const r=document.querySelector(s).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};};return {hud:b('.hud'),stage:b('.stage'),map:b('#map'),view:b('.view'),now:b('#mview')};});
 for(const width of [390,320]){
  const frames=[];
  for(const path of ['/baseline','/']){
   await open(page,width,844,path);await page.locator('#mnav [data-t="map"]').click();const map=await geometry();
   await page.locator('#mnav [data-t="now"]').click();const now=await geometry();
   await expect(page.locator('.m-tile[data-cell="h-stuck"]')).toBeVisible();await expect(page.locator('#mnav')).toBeVisible();
   expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);frames.push({map,now});
  }expect(frames[1]).toEqual(frames[0]);
 }
});
