/* Codex is light over the painted village: a separate, smooth canvas. */
const Codex = (() => {
  const palette = {cyan:'#5ef2ff', violet:'#a77bff', core:'#fff'};
  const gate = {x:1210,y:190};
  PLACES.codex = {id:'codex',n:'Codex Gate',x:1178,y:125,w:64,h:130,kind:'codex'};
  const layer = document.createElement('canvas');
  layer.id = 'codex-light'; layer.setAttribute('aria-hidden','true');
  Object.assign(layer.style,{position:'absolute',inset:'0',width:'100%',height:'100%',pointerEvents:'none'});
  cv.after(layer);
  const light = layer.getContext('2d');
  const style = document.createElement('style');
  style.textContent = `:root,[data-theme="light"],[data-theme="dark"]{--codex-cyan:#5ef2ff;--codex-violet:#a77bff}
    .codex-target{position:absolute;background:transparent;border:0;cursor:pointer;padding:0;color:transparent;z-index:2}
    .codex-target:focus-visible{outline:2px solid var(--codex-cyan);outline-offset:4px;border-radius:50%}
    .codex-inspector{font-family:monospace}.codex-inspector h2{color:var(--codex-cyan);text-shadow:0 0 12px var(--codex-violet)}
    .codex-inspector dt{color:var(--codex-violet);margin-top:12px}.codex-inspector dd{margin:3px 0;overflow-wrap:anywhere}
    .codex-inspector article{border-top:1px solid var(--hair);padding:8px 0}.codex-inspector h3{font:600 14px var(--body);overflow-wrap:anywhere}.codex-inspector p{font:13px/1.5 var(--body)}\n    .codex-swatch{background:linear-gradient(135deg,var(--codex-cyan),var(--codex-violet))!important;transform:rotate(45deg);box-shadow:0 0 8px var(--codex-cyan)}`;
  document.head.appendChild(style);
  const chip = document.createElement('button');chip.className='chip btn';
  chip.innerHTML='<i class="codex-swatch"></i>Codex (OpenAI)';chip.onclick=()=>inspect();$('legend').appendChild(chip);
  const builds = new Map(), workers = new Map(), buttons = new Map(), visibleTargets = new Set();
  let source = {sessions:[],quota:{}}, hits = [], epoch = 0;
  const elapsed = node => Math.max(0,performance.now()/1000-node.changed);
  const meta = id => source.sessions.find(s=>s.id===id) || {};
  function node(session){return {session:{...session},changed:performance.now()/1000,trail:[],x:gate.x,y:gate.y,takeover:false};}
  function load(data){
    source = data || {sessions:[],quota:{}};
    const seen = new Set();
    for(const session of source.sessions || []){
      if(session.kind==='doit-build'){
        const old=builds.get(session.id);
        if(!old)builds.set(session.id,node(session));
        else {old.session={...session};}
        continue;
      }
      seen.add(session.id);
      const old=workers.get(session.id);
      if(!old)workers.set(session.id,node(session));
      else {if(old.session.state!==session.state){old.changed=performance.now()/1000;old.trail=[];}old.session={...session};}
    }
    for(const [id,worker] of workers)if(!seen.has(id)&&worker.session.state!=='ended'){
      worker.session.state='ended';worker.changed=performance.now()/1000;
    }
  }
  function reset(){
    builds.clear();epoch++;
    for(const session of source.sessions || [])if(session.kind==='doit-build'&&toMs(session.started)<T0){
      const n=node(session);if(session.state==='live')n.changed-=5;builds.set(session.id,n);
    }
  }
  function event(e){
    if((e.ty==='build-started'||e.ty==='spawn-started')&&e.backend==='codex'){
      if(builds.has(e.sp))return;
      const session={...meta(e.sp),id:e.sp,kind:'doit-build',subject:e.s,role:e.r||roleOf(e.sp)||'builder',state:'live',started:e.t,last:e.t};
      const n=node(session);if(S.quiet)n.changed-=5;builds.set(e.sp,n);
    }
    const n=builds.get(e.sp);if(!n)return;
    if(e.ty==='backend-fallback'){
      n.session.state='fallback';n.session.fell_back='fell back: '+(e.why||'backend unavailable');n.session.fallback_at=e.t;
      n.session.last=e.t;n.changed=performance.now()/1000;n.trail=[];n.fallbackEvent=e;
      if(S.quiet||REDUCED)takeover(n);
    } else if(['build-done','spawn-done','spawn-failed','spawn-stale'].includes(e.ty)){
      if(n.session.state==='ended')return;
      n.session.state='ended';n.session.last=e.t;n.changed=performance.now()/1000;n.trail=[];
      if(e.tok!=null)n.session.tokens=e.tok;
      if(S.quiet)n.changed-=5;
      if(S.actors.has(e.sp)&&e.ty==='build-done')finishActor(e.sp,e.st==='DONE');
    }
  }
  function takeover(n){
    if(n.takeover)return;n.takeover=true;
    const e=n.fallbackEvent||{sp:n.session.id,s:n.session.subject,r:n.session.role,ms:S.now,t:n.session.fallback_at};
    spawnActor({...e,backend:null,cl:e.t},n.session.role||'builder');
  }
  let inspecting=false;
  function inspect(id){
    const html=()=>{
      const s=meta(id),q=source.quota||{};
      const seats=source.seats_today||[],workers=source.workers||[];
      const selected=s.id?`<h4>Selected marker</h4><p><b>${esc(s.subject)}</b> · ${esc(s.role)} · ${esc(s.outcome||s.state)}<br>Started ${esc(s.started)}${s.fell_back?'<br>'+esc(s.fell_back):''}</p>`:'';
      return `<div class="codex-inspector"><h2>Codex (OpenAI)</h2><div class="cls">Read-only · today is UTC · as of ${esc(source.at||'awaiting server')}</div>${selected}
        <h4>Ledger seats today (${seats.length})</h4>
        ${seats.length?`<table><tr><th>Role</th><th>Spec</th><th>Start (UTC)</th><th>Outcome</th></tr>${seats.map(s=>`<tr><td>${esc(s.role)}</td><td><a class="link" data-subj="${esc(s.subject)}" title="${esc(s.subject)}">${esc((specInfo(s.subject)||{}).title||s.subject)}</a><br><small>${esc(s.id)}</small></td><td>${esc((s.started||'').slice(11,16))}</td><td>${esc(s.outcome||s.state)}</td></tr>`).join('')}</table>`:'<p>No Codex-backed ledger seats started today.</p>'}
        <h4>Burn-down workers (${workers.length})</h4><p class="tiny">Done means REPORT.md exists; running means no report yet. This is not a process health check.</p>
        ${workers.map(w=>`<article><h3>${esc(w.name)} · ${esc(w.title)}</h3><b>${esc(w.state)}</b><p>${esc(w.summary||'Report has no prose paragraph yet.')}</p></article>`).join('')||'<p>No worker directories with a matching brief.</p>'}
        ${source.error?`<p>${esc(source.error)}</p>`:''}
        <h4>Weekly quota</h4><p>${q.weekly_used_pct==null?'Unavailable':esc(q.weekly_used_pct)+'% used'}</p></div>`;
    };
    openPanel(html(),{html,bind:bindLinks});bindLinks();
    if(!inspecting&&DATA&&DATA.live){
      inspecting=true;
      fetch(api('/api/codex'),{cache:'no-store'}).then(r=>{if(!r.ok)throw Error('unavailable');return r.json();})
        .then(d=>{load(d);rerenderPanel();}).catch(()=>{source.error='Codex information could not be refreshed.';rerenderPanel();})
        .finally(()=>{inspecting=false;});
    }
  }

  function target(id,x,y,size,label){
    visibleTargets.add(id);
    let b=buttons.get(id);
    if(!b){b=document.createElement('button');b.className='codex-target';b.dataset.codex=id;
      b.onclick=()=>inspect(['gate','ship'].includes(id)?undefined:id);
      b.addEventListener('keydown',ev=>{if(ev.key===' '||ev.key==='Enter')ev.stopPropagation();});
      cv.parentElement.appendChild(b);buttons.set(id,b);}
    b.setAttribute('aria-label',label);
    Object.assign(b.style,{left:(ox+(x-size/2)*scale)+'px',top:(oy+(y-size/2)*scale)+'px',width:Math.max(24,size*scale)+'px',height:Math.max(24,size*scale)+'px'});
    b.dataset.epoch=String(epoch);b.hidden=false;
  }
  function glow(x,y,r,strength=1){
    const halo=light.createRadialGradient(x,y,0,x,y,r);
    halo.addColorStop(0,`rgba(94,242,255,${.3*strength})`);halo.addColorStop(.45,`rgba(167,123,255,${.15*strength})`);halo.addColorStop(1,'rgba(167,123,255,0)');
    light.fillStyle=halo;light.fillRect(x-r,y-r,2*r,2*r);
  }
  function portal(t,active){
    light.save();light.globalAlpha=active?1:.27;glow(gate.x,gate.y,70,active?1:.25);
    const gradient=light.createLinearGradient(gate.x-32,gate.y-65,gate.x+32,gate.y+65);
    gradient.addColorStop(0,palette.cyan);gradient.addColorStop(1,palette.violet);
    light.strokeStyle=gradient;light.shadowColor=palette.cyan;light.shadowBlur=12;light.lineWidth=2;
    light.beginPath();light.ellipse(gate.x,gate.y,32,65,0,0,Math.PI*2);light.stroke();
    light.lineWidth=1;light.beginPath();light.ellipse(gate.x,gate.y,24,53,0,REDUCED?0:t*.25,(REDUCED?0:t*.25)+Math.PI*1.2);light.stroke();
    if(!REDUCED&&active)for(let i=0;i<18;i++){
      const phase=(t*.15+i/18)%1;light.globalAlpha=(1-phase)*.7;
      light.fillStyle=i%2?palette.cyan:palette.violet;light.beginPath();light.arc(gate.x+Math.sin(i*13)*38,gate.y+60-phase*140,1.2,0,7);light.fill();
    }
    light.globalAlpha=active?.9:.4;light.shadowBlur=0;light.fillStyle=palette.cyan;light.font='11px monospace';light.textAlign='center';light.fillText('CODEX GATE',gate.x,gate.y+85);light.restore();
    target('gate',gate.x,gate.y,68,'Codex Gate, OpenAI sessions and weekly quota');
  }
  function ship(t){
    const x=gate.x+(REDUCED?0:Math.sin(t*.18)*7),y=73+(REDUCED?0:Math.cos(t*.23)*4);
    target('ship',x,y,56,'Codex OpenAI — seats and burn-down workers');
    light.save();light.translate(x,y);glow(0,0,62,.8);light.strokeStyle=palette.cyan;
    light.shadowColor=palette.violet;light.shadowBlur=12;light.lineWidth=1.4;
    light.fillStyle='rgba(167,123,255,.15)';light.beginPath();light.moveTo(-57,0);light.lineTo(-34,18);light.lineTo(30,18);light.lineTo(55,-6);light.lineTo(-57,0);light.fill();light.stroke();
    light.beginPath();light.moveTo(-25,-4);light.lineTo(-8,-43);light.lineTo(3,-4);light.lineTo(22,-33);light.lineTo(28,-4);light.stroke();
    light.strokeStyle=palette.violet;light.beginPath();light.ellipse(0,-8,46,9,-.12,0,7);light.stroke();
    const quota=source.quota?.weekly_used_pct,used=quota==null?0:Math.max(0,Math.min(100,quota));
    light.shadowBlur=6;light.fillStyle=quota==null?palette.violet:used<70?'#63ff9c':used<90?'#ffca65':'#ff637c';
    light.fillRect(-32,10,64*(quota==null?1:used/100),3);light.strokeStyle=palette.cyan;light.strokeRect(-32,10,64,3);
    light.shadowBlur=0;light.font='10px monospace';light.textAlign='center';light.fillStyle=palette.core;light.fillText(quota==null?'QUOTA —':used+'% WEEKLY',0,34);light.restore();
  }
  function crystal(n,x,y,t,alpha){
    light.save();light.globalAlpha=alpha;
    if(!REDUCED){n.trail.push({x,y});if(n.trail.length>14)n.trail.shift();
      for(let i=0;i<n.trail.length;i++){light.globalAlpha=alpha*i/n.trail.length*.15;glow(n.trail[i].x,n.trail[i].y,15,.5);}light.globalAlpha=alpha;}
    glow(x,y,36);light.shadowColor=palette.cyan;light.shadowBlur=10;light.lineWidth=1.2;
    const gradient=light.createLinearGradient(x-10,y-16,x+10,y+16);gradient.addColorStop(0,palette.cyan);gradient.addColorStop(1,palette.violet);
    light.fillStyle='rgba(94,242,255,.18)';light.strokeStyle=gradient;light.beginPath();light.moveTo(x,y-17);light.lineTo(x+12,y);light.lineTo(x,y+17);light.lineTo(x-12,y);light.closePath();light.fill();light.stroke();
    light.beginPath();light.moveTo(x,y-17);light.lineTo(x+4,y);light.lineTo(x,y+17);light.moveTo(x-12,y);light.lineTo(x+12,y);light.stroke();
    glow(x,y,8,1.5);light.fillStyle=palette.core;light.beginPath();light.arc(x,y,2.4,0,7);light.fill();
    light.shadowBlur=0;light.strokeStyle=palette.violet;light.globalAlpha=alpha*.5;light.beginPath();light.ellipse(x,y,23,8,-.3,0,7);light.stroke();
    light.globalAlpha=alpha;for(let j=0;j<3;j++){const a=(REDUCED?0:t*1.3)+j*Math.PI*2/3;light.fillStyle=j%2?palette.violet:palette.cyan;light.beginPath();light.arc(x+23*Math.cos(a),y+8*Math.sin(a),1.6,0,7);light.fill();}
    light.font='10px monospace';light.textAlign='center';light.fillStyle=palette.cyan;
    const label=n.session.tmux||n.session.subject||n.session.id;light.fillText(label.slice(0,40),x,y+34);light.restore();
    n.x=x;n.y=y;hits.push({id:n.session.id,x,y,state:n.session.state});
    target(n.session.id,x,y,36,`Codex ${n.session.role||'worker'} for ${label}, ${n.session.state==='live'?'building':n.session.state}, ${fmtDur(Math.max(0,S.now-toMs(n.session.started)))}`);
  }
  function draw(t){
    if(layer.width!==cv.width||layer.height!==cv.height){layer.width=cv.width;layer.height=cv.height;}
    light.setTransform(1,0,0,1,0,0);light.clearRect(0,0,layer.width,layer.height);
    visibleTargets.clear();
    hits=[];if(!DATA||PHONE.matches&&mTab!=='map'){for(const b of buttons.values())b.hidden=true;return;}
    light.setTransform(cv._dpr,0,0,cv._dpr,0,0);light.translate(ox,oy);light.scale(scale,scale);
    light.globalCompositeOperation='lighter';
    const live=[...builds.values(),...workers.values()].some(n=>n.session.state==='live');portal(t,live);if(live)ship(t);
    let i=0;
    for(const n of builds.values()){
      const s=n.session,k=elapsed(n),fx=315+(i++%5-2)*30,fy=532;
      if(s.state==='fallback'){
        if(k<2&&!REDUCED){for(let j=0;j<24;j++){light.globalAlpha=(1-k/2);light.fillStyle=j%2?palette.violet:palette.cyan;light.beginPath();light.arc(fx+Math.sin(j*13)*k*28,fy+k*35+(j%6)*3,1.5,0,7);light.fill();}light.globalAlpha=1;}
        else takeover(n);continue;
      }
      if(s.state==='ended'&&(k>4||REDUCED))continue;
      const progress=REDUCED?1:Math.min(1,k/3),depart=s.state==='ended';
      const x=depart?fx+(gate.x-fx)*progress:gate.x+(fx-gate.x)*progress;
      const y=depart?fy+(gate.y-fy)*progress:gate.y+(fy-gate.y)*progress;
      crystal(n,x,y+(REDUCED?0:Math.sin(t*2+i)*4),t,depart?Math.max(0,1-k/4):1);
    }
    i=0;
    for(const n of workers.values()){
      const s=n.session,k=elapsed(n),ended=s.state==='ended';if(ended&&(k>4||REDUCED))continue;
      const a=(REDUCED?0:t*(s.state==='idle'?.08:.25))+(i++*2.4);
      const x=gate.x+Math.cos(a)*52-(ended?k*16:0),y=gate.y+Math.sin(a)*80-(ended?k*8:0);
      crystal(n,x,y,t*(s.state==='idle'?.3:1),ended?Math.max(0,1-k/4):s.state==='idle'?.4:1);
    }
    for(const [id,b] of buttons)b.hidden=!visibleTargets.has(id);
    light.globalAlpha=1;light.globalCompositeOperation='source-over';
  }
  return {load,reset,event,draw,inspect,get positions(){return hits.map(h=>({...h}));}};
})();
