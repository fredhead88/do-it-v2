/* Operator summaries are server projections; this module only presents them. */
const Operator = (() => {
  let source=null, pending=false, started=false;
  const label=s=>`<a class="link" data-subj="${esc(s.spec)}" title="${esc(s.spec)} · ${esc(s.title)}">${esc(s.title)}</a>`;
  function duration(minutes){
    if(typeof minutes!=='number'||!Number.isFinite(minutes))return 'ETA unavailable';
    if(minutes<60)return 'about '+Math.max(1,Math.round(minutes/5)*5)+' min';
    if(minutes<48*60)return 'about '+(Math.round(minutes/30)/2)+' h';
    return 'about '+Math.round(minutes/1440)+' days';
  }
  function wait(minutes){
    if(typeof minutes!=='number'||!Number.isFinite(minutes))return 'unavailable';
    if(minutes<90)return Math.round(minutes)+' min';
    if(minutes<2880)return Math.round(minutes/60)+' h';
    return Math.round(minutes/1440)+' days';
  }
  function valid(){return source&&!source.error&&LIVE&&Date.now()-Date.parse(source.at)<120000;}
  function summaryHtml(d){
    const next=d.next.map(s=>`${label(s)} (${duration(s.eta_min)})`).join(' → ')||'nothing queued';
    const priorities=d.priorities.map(g=>`<span class="priority-group"><b>${esc(g.name)}:</b> ${g.specs.map(s=>`<a class="link" data-subj="${esc(s.spec)}" title="${esc(s.spec)} · ${esc(s.title)}">${esc(s.stage)}</a>`).join('; ')}</span>`).join(' · ');
    const needs=d.needs_you.map(s=>`${label(s)} — ${esc(s.asks)}`).join('; ')||'nothing';
    const stuck=d.stuck.map(s=>`${label(s)} — idle ${wait(s.idle_min)}, limit ${wait(s.limit_min)}`).join('; ')||'nothing';
    const held=d.held.length?` · <button class="summary-link" data-operator="held">${d.held.length} deliberately waiting</button>`:'';
    const k=d.keeping_up;
    return `<div class="know-row next-merges" title="${esc(d.eta_note)}"><button class="summary-link" data-operator="next">Next to go live:</button> ${next} <span class="tiny">· merge estimates; deployment follows</span></div>
      <div class="know-row priorities"><button class="summary-link" data-operator="priorities">Priorities:</button> ${priorities}</div>
      <div class="know-row"><b>Needs you:</b> ${needs}</div>
      <div class="know-row"><button class="summary-link" data-operator="stuck">Actually stuck:</button> ${stuck}${d.notices.length?' · some rule evidence unavailable':''}${held}</div>
      <div class="know-row"><b>Keeping up?</b> ${k.merged} merged today vs ${k.added} added to merge queue · queue ${esc(k.trend)} (${k.queue} waiting) · today = UTC</div>`;
  }
  function render(){
    const target=$('operator-lines');if(!target)return;
    let html;
    if(!LIVE)html='<div class="know-row">Switch to LIVE for current priorities, queue and operator decisions.</div>';
    else if(valid())html=summaryHtml(source);
    else html=`<div class="know-row">${esc(source&&source.error||(pending&&!source?'Reading your priorities and merge queue…':'Operator information unavailable — waiting for a fresh server reading.'))}</div>`;
    if(target.innerHTML!==html){target.innerHTML=html;bind(target);}
  }
  function bind(target){
    for(const a of target.querySelectorAll('[data-subj]'))a.onclick=()=>openSubject(a.dataset.subj);
    for(const a of target.querySelectorAll('[data-operator]'))a.onclick=()=>inspect(a.dataset.operator);
  }
  function rows(items){return items.map(s=>`<p>${label(s)}<br>${esc(s.stage)}${s.reason?' · '+esc(s.reason):''}</p>`).join('')||'<p>Nothing.</p>';}
  function shelfHtml(){
    if(!valid())return '<p>Shelf information unavailable in this view. Return to LIVE for a fresh reading.</p>';
    const s=source.shelf;
    return `<h4>Written, held (${s.held.length})</h4>${rows(s.held)}<h4>Written, ready, no builder yet (${s.ready.length})</h4>${rows(s.ready)}${s.other.length?`<h4>Written, awaiting another step (${s.other.length})</h4>${rows(s.other)}`:''}`;
  }
  function inspect(kind){
    const html=()=>{
      if(!valid())return '<h2>Operator information unavailable</h2><p>Return to LIVE for a fresh reading.</p>';
      if(kind==='next')return '<h2>Next to go live</h2><p>'+esc(source.eta_note)+'</p>'+rows(source.next.map(s=>({...s,stage:`Position ${s.position} · merge ${duration(s.eta_min)}`})));
      if(kind==='shelf')return '<h2>Shelf</h2>'+shelfHtml();
      if(kind==='priorities')return '<h2>Operator priorities</h2>'+source.priorities.map(g=>`<h4>${esc(g.name)}</h4>${rows(g.specs)}`).join('');
      if(kind==='held')return '<h2>Deliberately waiting</h2>'+rows(source.held);
      return '<h2>Actually stuck</h2>'+rows(source.stuck.map(s=>({...s,stage:`Idle ${wait(s.idle_min)}; rule ${s.rule} allows ${wait(s.limit_min)}`})))+source.notices.map(n=>`<p>${esc(n.spec)}: ${esc(n.reason)}</p>`).join('');
    };
    openPanel(html(),{html,bind:()=>bind($('p-body'))});bind($('p-body'));
  }
  async function refresh(){
    if(pending||!DATA||!DATA.live)return;
    pending=true;
    try{const r=await fetch(api('/api/operator'),{cache:'no-store'});if(!r.ok)throw Error('server unavailable');source=await r.json();}
    catch(_){source={error:'Operator information unavailable — server did not answer.'};}
    finally{pending=false;render();renderShelfTile();rerenderPanel();}
  }
  function renderShelfTile(){
    if(!valid())return;
    const s=source.shelf,el=$('h-shelf');
    el.firstElementChild.textContent=`${s.held.length} held · ${s.ready.length} ready, no builder yet${s.other.length?' · '+s.other.length+' already being handled':''}`;
    $('h-stuck').firstElementChild.textContent=String(source.stuck.length)+(source.notices.length?' (partial)':'');
    $('h-stuck').className='stat click '+(source.stuck.length||source.notices.length?'warn':'good');
    el.lastElementChild.textContent='Written specs — held vs ready';el.className='stat click';
  }
  function start(){
    if(started)return;started=true;
    refresh();setInterval(refresh,30000);setInterval(render,1000);
    $('h-shelf').onclick=()=>inspect('shelf');$('h-stuck').onclick=()=>inspect('stuck');
  }
  return {start,refresh,render,summaryHtml,shelfHtml,inspect,duration,renderShelfTile,load(d){source=d;render();}};
})();
