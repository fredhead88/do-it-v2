// Run with the realm's existing Playwright installation; no package installation needed.
const {test, expect} = require('playwright/test');
// Fixture boot and screenshots share the test budget on a busy build box.
test.setTimeout(120_000);
const {spawn, execFileSync} = require('node:child_process');
const {mkdirSync, mkdtempSync, writeFileSync} = require('node:fs');
const {tmpdir} = require('node:os');
const {join, resolve} = require('node:path');
const {createHash} = require('node:crypto');

const root = resolve(__dirname, '..');
const evidence = process.env.CODEX_EVIDENCE_DIR || mkdtempSync(join(tmpdir(), 'realm-codex-evidence-'));
mkdirSync(evidence, {recursive:true});
const python = String.raw`
import sys, os, json, pathlib, tempfile, time, http.server, urllib.parse
sys.path.insert(0, 'src')
import test_realm as fixtures
import realm, realm_codex
home = pathlib.Path(tempfile.mkdtemp(prefix='realm-codex-fixture-'))
folder = home / '.codex/sessions/2026/10/12'
folder.mkdir(parents=True)
now = time.time()
iso = realm_codex.stamp
start = {'ts':iso(now-600), 'type':'build-started', 'spawn':'L-builder-7', 'subject':'L-spec-7', 'backend':'codex'}
fallback = {'ts':iso(now-60), 'type':'backend-fallback', 'spawn':'L-builder-7', 'subject':'L-spec-7', 'why':'weekly limit'}
ended = {'ts':iso(now-30), 'type':'build-done', 'spawn':'L-builder-7', 'subject':'L-spec-7', 'status':'DONE'}
rollout = [
 {'type':'session_meta','payload':{'id':'worker-live','cwd':str(home / '.do-it/worktrees/do-it-v2/audit'),'timestamp':iso(now-120)}},
 {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'# Technical audit\nFAKE_TOKEN_SECRET_MARKER'}]}},
 {'type':'turn_context','payload':{'turn_id':'turn-1'}},
 {'type':'event_msg','payload':{'type':'token_count','rate_limits':{'secondary':{'used_percent':71,'window_minutes':10080,'resets_at':int(now+3600)},'credits':{'balance':'20.00'}},'info':{'total_token_usage':{'total_tokens':1234}}}}
]
f = folder / 'rollout-live.jsonl'
f.write_text('\n'.join(map(json.dumps,rollout)))
original = realm_codex.payload
realm_codex.payload = lambda rows, **kw: original(rows,home=home,now=now,processes={'worker-live'},windows={rollout[0]['payload']['cwd']:'codex-audit'})
realm.sys_signals = lambda: {}
realm.maker_index = lambda *a: {}
(fixtures.TMP / 'content').mkdir(exist_ok=True)
(fixtures.TMP / 'content/L-spec-7.md').write_text('---\ntitle: Shared fixture spec\ncharter: L-charter-0064\n---\n# Shared fixture spec\n## Requirements\nAC1 [ui]: Show <img src=x onerror=alert(1)> as text.\nAC2 [backend]: Keep the shared detail.\n')
def data(phase):
 rows = [] if phase=='empty' else [start]
 if phase in ('fallback','ended'): rows.append(fallback)
 if phase=='ended': rows.append(ended)
 fixtures.w('L-builder-7.jsonl',rows)
 # Phase files are rewritten, so append-only ledger and glance caches must reset.
 realm.LEDGER = realm.realm_ledger.Ledger()
 realm._GLANCE['key'] = None
 d=realm.bootstrap(0,states=[['L-spec-7','building']],goals=[],proving=[])
 d['lane']=[{'spec':'L-spec-7','bucket':'building','reason':'fixture builder working','rule':'builder','since':iso(now-600)}]
 d.update(live=False,now=iso(now),window=[iso(now-1800),iso(now)])
 if phase=='empty': d['codex']={'sessions':[],'quota':{}}
 return d
class Handler(realm.Handler):
 def do_GET(self):
  u=urllib.parse.urlparse(self.path);q=urllib.parse.parse_qs(u.query)
  phase=q.get('phase',['live'])[0]
  if u.path in ('/api/bootstrap','/fixture/backend'): self._json(data(phase));return
  if u.path=='/baseline':
   body=pathlib.Path(sys.argv[1]).read_bytes().replace(b'/*__CODEX_JS__*/',pathlib.Path('src/realm_codex.js').read_bytes());self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
  if u.path=='/api/since': self._json({'seq':0,'ledger':[],'claims':[]});return
  if u.path=='/api/spec': return super().do_GET()
  if u.path.startswith('/api/'): self._json({});return
  super().do_GET()
httpd=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
print(httpd.server_address[1],flush=True)
httpd.serve_forever()
`;
let server, base, baseline;
test.beforeAll(async () => {
  baseline = join(evidence, 'baseline.html');
  const ref=process.env.CODEX_BASE_REF || execFileSync('git',['rev-parse','5b75a38^'],{cwd:root}).toString().trim();
  writeFileSync(baseline,execFileSync('git',['show',ref+':src/realm.html'],{cwd:root}));
  server = spawn('/usr/bin/python3',['-u','-c',python,baseline],{cwd:root,stdio:['ignore','pipe','pipe']});
  base = await new Promise((done,fail)=>{
    let output='';server.stdout.on('data',chunk=>{output+=chunk;const match=/^(\d+)\n/.exec(output);if(match)done('http://127.0.0.1:'+match[1]);});
    server.on('exit',code=>fail(new Error('fixture server exited '+code)));
    server.stderr.on('data',chunk=>process.stderr.write(chunk));
  });
});
test.afterAll(()=>{server?.kill();});

async function open(page, phase='live', path='/') {
  await page.route('**/*', async route=>{
    const url=new URL(route.request().url());
    if(url.origin!==base)return route.abort();
    if(url.pathname==='/api/bootstrap')return route.continue({url:base+'/api/bootstrap?phase='+phase});
    return route.continue();
  });
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+path);
  await page.waitForFunction(()=>typeof DATA!=='undefined'&&DATA&&DATA.now);
  await expect(page.locator('#status')).toBeHidden();
  if(page.viewportSize().width<=700)await page.locator('#mnav [data-t="map"]').click();
  await page.evaluate(()=>{LIVE=false;seek(toMs(DATA.now));S.playing=false;});
  await page.waitForTimeout(100);
  expect(errors).toEqual([]);
}
async function shot(page,name) {await page.screenshot({path:join(evidence,name+'.png'),fullPage:true});}

test('signed fixture responses: lifecycle, worker privacy and path shortening',async ({request})=>{
  const signatures=[];
  for(const [phase,state] of [['live','live'],['fallback','fallback'],['ended','ended']]){
    const response=await request.get(base+'/fixture/backend?phase='+phase),body=await response.body(),data=JSON.parse(body);
    expect(response.status()).toBe(200);
    const build=data.codex.sessions.find(s=>s.kind==='doit-build');expect(build.state).toBe(state);
    if(phase!=='live')expect(build.fell_back).toBe('fell back: weekly limit');
    const worker=data.codex.sessions.find(s=>s.kind==='worker');
    expect(worker).toMatchObject({subject:'Technical audit',state:'live',tokens:1234,turns:1,cwd:'audit',tmux:'codex-audit'});
    expect(JSON.stringify(data.codex)).not.toContain('FAKE_TOKEN_SECRET_MARKER');
    expect(JSON.stringify(data.codex)).not.toContain('/');
    signatures.push({url:response.url(),status:response.status(),body_sha256:createHash('sha256').update(body).digest('hex'),body_excerpt:JSON.stringify(data.codex)});
  }
  writeFileSync(join(evidence,'backend-responses.json'),JSON.stringify(signatures,null,2));
});

for(const width of [1440,400]){
  test('AC5 codex-built spec opens the spec panel with a session section at '+width+'px',async ({page})=>{
    await page.setViewportSize({width,height:900});
    await page.context().tracing.start({screenshots:true,snapshots:true});
    await open(page);
    const construct=page.locator('[data-codex="L-builder-7"]');
    await expect(construct).toBeVisible();
    const box=await construct.boundingBox();
    await page.mouse.click(box.x+box.width/2,box.y+box.height/2);
    await assertSpecPanel(page);
    const session=page.locator('#p-body .codex-inspector');
    await expect(session).toContainText('CODEX SESSION');
    for(const label of ['Kind','Subject / title','Tmux window','State','Started','Last activity','Turns','Tokens','Fallback history','Weekly quota','Resets','Worktree'])await expect(session).toContainText(label);
    await expect(page.locator('#p-body img')).toHaveCount(0);
    const headings=await page.locator('#p-body h2,#p-body h4').allTextContents();
    expect(headings).toEqual(['L-spec-7 · Shared fixture spec','STAGE','LANE STATE','CRITERIA','CHARTER','LATEST EVENTS','CODEX SESSION']);
    await shot(page,'AC5-'+width);
    // All spec links use the same entry point and choose the latest build session.
    await page.evaluate(()=>{DATA.codex.sessions.push({...DATA.codex.sessions.find(s=>s.kind==='doit-build'),id:'newer',last:'2099-01-01T00:00:00Z',tmux:'latest-build'});openSubject('L-spec-7');});
    await expect(page.locator('#p-body')).toContainText('latest-build');
    await page.context().tracing.stop({path:join(evidence,'AC5-'+width+'.trace.zip')});
  });
  test('gate, ship, forge, inspector and fallback at '+width+'px',async ({page})=>{
    await page.clock.install();
    await page.setViewportSize({width,height:1000});await page.context().tracing.start({screenshots:true,snapshots:true});
    await open(page);
    await expect(page.locator('[data-codex="L-builder-7"]')).toBeVisible();
    const position=await page.evaluate(()=>Codex.positions.find(p=>p.id==='L-builder-7'));
    expect(position.y).toBeLessThan(540);expect(position.x).toBeGreaterThan(240);expect(position.x).toBeLessThan(390);
    await shot(page,'live-'+width);
    const box=await page.locator('[data-codex="L-builder-7"]').boundingBox();
    await page.mouse.click(box.x+box.width/2,box.y+box.height/2);
    const inspectorText=await page.locator('.codex-inspector').innerText();
    for(const label of ['Kind','Subject / title','Tmux window','State','Started','Last activity','Turns','Tokens','Fallback history','Weekly quota','Resets'])expect(inspectorText).toContain(label);
    await shot(page,'inspector-'+width);await page.locator('#p-close').click();
    // Hold each handover phase while screenshots are captured, even under load.
    // Allow a full test budget for the browser round trip before pausing.
    await page.clock.pauseAt(await page.evaluate(()=>Date.now())+120_000);
    await page.evaluate(()=>{const e={ty:'backend-fallback',sp:'L-builder-7',s:'L-spec-7',why:'weekly limit',r:'builder',t:new Date(S.now).toISOString(),ms:S.now};handle(e);});
    await page.clock.fastForward(250);expect(await page.evaluate(()=>S.actors.has('L-builder-7'))).toBe(false);
    await shot(page,'dissolve-'+width);
    await page.clock.fastForward(2200);expect(await page.evaluate(()=>S.actors.has('L-builder-7'))).toBe(true);
    await shot(page,'handover-'+width);
    await page.context().tracing.stop({path:join(evidence,'live-'+width+'.trace.zip')});
  });
}

async function assertSpecPanel(page) {
  const panel=page.locator('#p-body');
  for(const text of ['Shared fixture spec','STAGE','in flight','LANE STATE','fixture builder working','AC1','Show <img src=x onerror=alert(1)> as text.','AC2','Keep the shared detail.','LATEST EVENTS'])await expect(panel).toContainText(text);
  await expect(panel.locator('.tl')).toBeVisible();
}

test('AC4 and AC6 detail error states and session routing',async ({page})=>{
  await page.setViewportSize({width:1440,height:900});
  await page.context().tracing.start({screenshots:true,snapshots:true});
  await open(page);
  await page.evaluate(()=>{
    S.lane=[];S.subjectEvents.set('L-spec-7',Array.from({length:41},(_,i)=>({ms:S.now+i,ty:'fixture-event-'+i})));
    DATA.specs['L-spec-7'].criteria=[];DATA.specs['L-spec-7'].criteria_error='unparseable fixture';
    window.realmOpenSpec('L-spec-7',{title:'CODEX SESSION',rows:[['State','live<script>']]});
  });
  const panel=page.locator('#p-body');
  await expect(panel).toContainText('no lane row');
  await expect(panel).toContainText('criteria unreadable: unparseable fixture');
  await expect(panel.locator('.criteria')).toHaveCount(0);
  await expect(panel.locator('.tl li')).toHaveCount(40);
  await expect(panel).toContainText('live<script>');
  await expect(panel.locator('script')).toHaveCount(0);
  await shot(page,'AC4-error');
  await page.evaluate(()=>{DATA.specs['L-spec-7'].criteria_error=null;openSubject('L-spec-7');});
  await expect(panel).toContainText('no criteria listed');
  const requests=[];page.on('request',r=>{if(r.url().includes('/api/spec'))requests.push(r.url());});
  await page.evaluate(()=>{
    DATA.codex.sessions=DATA.codex.sessions.filter(s=>s.kind!=='doit-build');
    DATA.codex.sessions.push({id:'worker-spec',kind:'worker',subject:'L-spec-7'});
    openSubject('L-spec-7');
  });
  await expect(panel).not.toContainText('CODEX SESSION');
  for(const subject of ['L-charter-0064','L-spec-7-extra','x-L-spec-7','a-slug']){
    await page.evaluate(subject=>{Codex.load({sessions:[{id:'non-spec',kind:'worker',subject}],quota:{}});Codex.inspect('non-spec');},subject);
    await expect(panel).toContainText(subject);
    await expect(panel).not.toContainText('CRITERIA');
  }
  await page.locator('#p-close').click();
  await page.locator('[data-codex="gate"]').click();
  await expect(panel).toContainText('Codex Gate');
  expect(requests).toEqual([]);
  await shot(page,'AC6-gate');
  await page.context().tracing.stop({path:join(evidence,'AC4-AC6.trace.zip')});
});

test('AC7 claude actor opens the same spec panel with actor facts',async ({page})=>{
  await page.setViewportSize({width:1440,height:900});
  await page.context().tracing.start({screenshots:true,snapshots:true});
  await open(page);
  const actor=async(subject)=>{
    const point=await page.evaluate(subject=>{
      S.actors.clear();S.blockers=[];
      spawnActor({sp:'L-builder-claude',s:subject,ms:S.now,pr:'do-it-v2'},'builder');
      const a=S.actors.get('L-builder-claude');a.x=860;a.y=500;a.state='working';
      const r=cv.getBoundingClientRect();return {x:r.left+ox+(a.x+SW/2)*scale,y:r.top+oy+(a.y+SH/2)*scale};
    },subject);
    await page.mouse.click(point.x,point.y);
    await page.locator('#bb-box [data-open="actor:L-builder-claude"]').click();
  };
  await actor('L-spec-7');
  await assertSpecPanel(page);
  const panel=page.locator('#p-body');
  await expect(panel.locator('.codex-inspector')).toHaveCount(0);
  for(const label of ['ACTOR FACTS','Role','Model','Project','State','Set out','Elapsed','Claim'])await expect(panel.locator('.actor-facts')).toContainText(label);
  await shot(page,'AC7-actor');
  await page.locator('#p-close').click();
  await actor('L-charter-0064');
  await expect(panel).toContainText('THE PACKET CARRIES');
  await expect(panel).not.toContainText('CRITERIA');
  await shot(page,'AC7-non-spec');
  await page.context().tracing.stop({path:join(evidence,'AC7.trace.zip')});
});

test('reduced motion is steady, focusable, and departures finish',async ({page})=>{
  await page.emulateMedia({reducedMotion:'reduce'});await page.context().tracing.start({screenshots:true,snapshots:true});await open(page);
  const pixels=()=>page.evaluate(()=>document.querySelector('#codex-light').toDataURL());
  const first=await pixels();await page.waitForTimeout(200);expect(await pixels()).toEqual(first);
  await page.locator('[data-codex="worker-live"]').focus();await page.keyboard.press('Enter');await expect(page.locator('.codex-inspector')).toContainText('codex-audit');
  await shot(page,'reduced-motion');await page.locator('#p-close').click();
  await page.evaluate(()=>handle({ty:'build-done',sp:'L-builder-7',s:'L-spec-7',st:'DONE',t:new Date(S.now).toISOString(),ms:S.now}));
  await expect(page.locator('[data-codex="L-builder-7"]')).toBeHidden();
  await page.context().tracing.stop({path:join(evidence,'reduced-motion.trace.zip')});
});

test('empty Codex layer leaves village pixels within tolerance of base',async ({browser})=>{
  // Isolate the empty-layer comparison from prior session and storage state.
  const context=await browser.newContext();
  const page=await context.newPage();
  try {
    // Keep even the original header/panel/log layout above scale 1; AC9 then
    // preserves every base village font size in both comparison frames.
    await page.setViewportSize({width:2400,height:1800});
    await page.emulateMedia({reducedMotion:'reduce'});await page.context().tracing.start({screenshots:true,snapshots:true});await open(page,'empty');
    const village=async ()=>Buffer.from(await page.evaluate(()=>{
      // The world canvas retains antialiased edge pixels across frames. Compare
      // one fresh, paused frame at a fixed animation time on both versions.
      g.setTransform(1,0,0,1,0,0);g.clearRect(0,0,W,H);frame(0,0);
      if(typeof Codex!=='undefined')Codex.draw(0);
      const pixels=g.getImageData(0,0,1140,H).data;
      let binary='';for(let i=0;i<pixels.length;i+=8192)binary+=String.fromCharCode(...pixels.subarray(i,i+8192));
      return btoa(binary);
    }),'base64');
    // Independent base renders vary by up to 5/765 in summed RGB distance.
    // Allow 6/765 per pixel; every pixel and its alpha must remain within budget.
    const tolerance=6;
    const compare=(a,b)=>{
      expect(a.length).toBe(1140*800*4);expect(b.length).toBe(a.length);
      let max_rgb_distance=0,pixels_over_tolerance=0,alpha_mismatches=0;
      for(let i=0;i<a.length;i+=4){
        const distance=Math.abs(a[i]-b[i])+Math.abs(a[i+1]-b[i+1])+Math.abs(a[i+2]-b[i+2]);
        max_rgb_distance=Math.max(max_rgb_distance,distance);
        if(distance>tolerance)pixels_over_tolerance++;
        if(a[i+3]!==b[i+3])alpha_mismatches++;
      }
      return {max_rgb_distance,pixels_over_tolerance,alpha_mismatches};
    };
    const current=await village();
    await expect(page.locator('[data-codex="gate"]')).toBeVisible();
    // Draw retains hidden hit targets for reuse; only visible targets paint a layer.
    await expect(page.locator('.codex-target:visible')).toHaveCount(1);
    expect(await page.evaluate(()=>Codex.positions)).toEqual([]);
    await shot(page,'empty');
    await open(page,'empty','/baseline');
    const original=await village();
    await shot(page,'base-village');
    await open(page,'empty','/baseline');
    const control=await village(),diff=compare(current,original),base_control=compare(original,control);
    writeFileSync(join(evidence,'village-diff.json'),JSON.stringify({
      current_sha256:createHash('sha256').update(current).digest('hex'),
      base_sha256:createHash('sha256').update(original).digest('hex'),
      region:[0,0,1140,800],pixels:1140*800,tolerance_rgb_sum:tolerance,
      diff,base_control,within_tolerance:diff.pixels_over_tolerance===0&&diff.alpha_mismatches===0
    },null,2));
    await page.context().tracing.stop({path:join(evidence,'empty.trace.zip')});
    expect(base_control.pixels_over_tolerance).toBe(0);expect(base_control.alpha_mismatches).toBe(0);
    expect(diff.pixels_over_tolerance).toBe(0);expect(diff.alpha_mismatches).toBe(0);
  } finally {await context.close();}
});

for(const width of [1440,390])for(const expanded of [false,true]){
  test('glance waits for bootstrap before restoring '+expanded+' at '+width+'px',async ({page})=>{
    await page.setViewportSize({width,height:900});
    await page.context().tracing.start({screenshots:true,snapshots:true});
    await page.addInitScript(value=>localStorage.setItem('realm.glance.expanded',String(value)),expanded);
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    let release;const pending=new Promise(resolve=>{release=resolve;});
    await page.route('**/*',route=>new URL(route.request().url()).origin===base?route.continue():route.abort());
    await page.route('**/api/bootstrap',async route=>{await pending;await route.continue();});
    await page.goto(base);
    await page.waitForFunction(()=>typeof DATA!=='undefined'&&DATA===null);
    const toggle=page.locator('#glance-toggle');
    try{
      await expect(toggle).toBeDisabled();
      await expect(toggle).not.toHaveAttribute('aria-expanded',/true|false/);
      await expect(page.locator('#glance-lines')).toBeHidden();
    }finally{release();}
    await expect(toggle).toHaveAttribute('aria-expanded',String(expanded));
    expect(await page.evaluate(()=>DATA!==null)).toBe(true);
    await expect(toggle).toBeEnabled();
    await expect(page.locator('#status')).toBeHidden();
    if(width<=700)await page.locator('#mnav [data-t="map"]').click();
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded',String(!expanded));
    await shot(page,'glance-ready-'+width+'-'+expanded);
    expect(errors).toEqual([]);
    await page.context().tracing.stop({path:join(evidence,'glance-ready-'+width+'-'+expanded+'.trace.zip')});
  });
}
