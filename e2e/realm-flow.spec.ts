// Isolated canonical CLI fixture; uses the existing Playwright runner when available.
const {test,expect}=require('playwright/test');
const {spawn}=require('node:child_process');
const {resolve}=require('node:path');
const root=resolve(__dirname,'..');
test.setTimeout(90_000);
const python=String.raw`
import sys,http.server,threading,time,urllib.parse
sys.path.insert(0,'src')
import test_realm as fixture
import realm
realm.sys_signals=lambda: {}
realm.maker_index=lambda *a: {}
realm.omens_model=lambda *a: {}
realm.glance_payload=lambda *a: {}
realm.codex_payload=lambda *a: {'sessions':[], 'quota':{}}
realm.HUB.states=[]
with fixture.views_stub() as (repo,configure):
 class Handler(realm.Handler):
  def do_POST(self):
   if urllib.parse.urlparse(self.path).path=='/fixture/break':
    # Change the CLI output. The normal cache expires on the next sampler window.
    (repo/'next.json').write_text('{"stdout":"unavailable","exit":2}')
    (repo/'health.json').write_text('{"stdout":"unavailable","exit":2}')
    self._json({'ok':True});return
   super().do_POST()
 server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
 def sample():
  while True:
   realm.sample_once()
   time.sleep(1)
 threading.Thread(target=sample,daemon=True).start()
 print(server.server_port,flush=True)
 server.serve_forever()
`;
let server,base;
test.beforeAll(async()=>{
 server=spawn('/usr/bin/python3',['-u','-c',python],{cwd:root,stdio:['ignore','pipe','pipe']});
 const port=await new Promise((ok,no)=>{
  let output='';server.stdout.on('data',chunk=>{output+=chunk;const m=output.match(/^(\d+)\n/m);if(m)ok(m[1]);});
  server.once('error',no);server.once('exit',code=>no(Error('fixture exited '+code)));
 });
 base='http://127.0.0.1:'+port;
});
test.afterAll(()=>server?.kill());
test('flow refresh replaces counts with unknown after CLI failure',async({page,request})=>{
 await page.goto(base);
 if(!await page.locator('#b-flow').isVisible())await page.locator('#b-menu').click();
 await page.locator('#b-flow').click();
 const panel=page.locator('#p-body');
 for(const [stage,count] of Object.entries({idea:1,planned:1,building:2,checking:1,merging:1,live:'unknown'})){
  await expect(panel).toContainText(stage+' · '+count);
 }
 await expect(panel.locator('.stuck-row')).toHaveCount(3);
 await expect(panel).toContainText('operator');await expect(panel).toContainText('executor');
 await expect(page.locator('#h-stuck b')).toHaveText('3');
 await request.post(base+'/fixture/break');
 await expect(page.locator('#h-stuck b')).toHaveText('unknown',{timeout:40_000});
 for(const stage of ['idea','planned','building','checking','merging','live'])await expect(panel).toContainText(stage+' · unknown');
 await expect(panel.locator('.stuck-row')).toHaveCount(0);
 if(!await page.locator('#b-health').isVisible())await page.locator('#b-menu').click();
 await page.locator('#b-health').click();await expect(panel).toContainText('finishing per day · unknown');
});
