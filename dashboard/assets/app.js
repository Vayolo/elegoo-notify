/* ==========================================================================
   elegoo-notify webui v2 — logica applicativa (vanilla JS, zero build)
   Sezioni: utils/auth · toasts · SSE/state · hero · comandi · webcam ·
            chart · tabs · modelli+thumb · viewer · slice · file · eventi · AI
   ========================================================================== */
'use strict';

/* ============================ UTILS ============================ */
const $=id=>document.getElementById(id);
const esc=s=>{const d=document.createElement('span');d.textContent=String(s);return d.innerHTML};
const fmtETA=s=>{if(s==null)return '—';const m=Math.round(s/60);if(m<60)return m+' min';return Math.floor(m/60)+'h '+('0'+m%60).slice(-2)+'m'};
const fmtMB=b=>b>=1048576?(b/1048576).toFixed(1)+' MB':(b/1024).toFixed(0)+' KB';
function timeago(ts){const d=Date.now()/1000-ts;if(d<60)return 'adesso';if(d<3600)return Math.round(d/60)+' min fa';if(d<86400)return Math.round(d/3600)+' h fa';return Math.round(d/86400)+' g fa'}
function fmtClock(ts){return new Date(ts*1000).toLocaleTimeString('it-IT',{hour:'2-digit',minute:'2-digit'})}

/* ---- fetch con auth + content-type + error toast ---- */
let AUTH=null;
async function jfetch(url,opts={}){
  const h=Object.assign({},opts.headers||{});
  if(AUTH)h['Authorization']='Basic '+AUTH;
  if(opts.body&&typeof opts.body==='string'&&!h['Content-Type'])h['Content-Type']='application/json';
  const r=await fetch(url,Object.assign({},opts,{headers:h}));
  if(r.status===401){UI.openModal('authModal');throw new Error('401');}
  return r;
}
function tryAuth(){
  const u=$('authUser').value.trim(),p=$('authPass').value;
  if(!u||!p)return;
  AUTH=btoa(u+':'+p);
  localStorage.setItem('elegoo_auth',AUTH);
  UI.closeModal('authModal');
  boot();
}
AUTH=localStorage.getItem('elegoo_auth');

/* ============================ TOASTS ============================ */
function toast(title,msg='',type='info',ms=4200){
  const t=document.createElement('div');t.className='toast '+type;
  const ico={ok:'✅',err:'⛔',info:'ℹ️'}[type]||'ℹ️';
  t.innerHTML=`<span class="tico">${ico}</span><span class="tbody"><b>${esc(title)}</b>${msg?esc(msg):''}</span>`;
  $('toasts').appendChild(t);
  setTimeout(()=>{t.classList.add('out');setTimeout(()=>t.remove(),350)},ms);
}

/* ============================ UI helpers ============================ */
const UI={
  openModal(id){$(id).classList.add('open')},
  closeModal(id){$(id).classList.remove('open')},
  badge(txt,cls){return `<span class="badge ${cls}">${esc(txt)}</span>`}
};
document.querySelectorAll('.modal').forEach(m=>m.addEventListener('click',e=>{if(e.target===m)m.classList.remove('open')}));
$('authGo').onclick=tryAuth;
$('authPass').addEventListener('keydown',e=>{if(e.key==='Enter')tryAuth()});

/* ============================ STATE / SSE ============================ */
let state=null;
const TEMPS={t:[],noz:[],nozT:[],bed:[],bedT:[]};
const EVENTS=[];const MAXEV=120;
let evFilter='all';

function pushEvent(ev){
  EVENTS.unshift(ev);if(EVENTS.length>MAXEV)EVENTS.pop();
  renderEvents();
}

function setState(s){
  state=s;
  $('connChip').classList.toggle('on',!!s.connected);
  $('connTxt').textContent=s.connected?'connessa':'offline';
  const st=s.status||'—';
  $('stState').textContent=st;
  const badgeMap={printing:['in stampa','ok'],paused:['in pausa','warn'],complete:['completata','info'],
    stopped:['fermata','warn'],failed:['errore','err'],idle:['in attesa','dim'],disconnected:['offline','err']};
  const bkey=Object.keys(badgeMap).find(k=>st.includes(k))||'idle';
  $('stBadge').outerHTML=`<span class="badge ${badgeMap[bkey][1]}" id="stBadge">${badgeMap[bkey][0]}</span>`;
  $('stFile').textContent=s.filename?('📄 '+(s.filename||'')):'nessun file';
  const pct=s.percent;
  $('stPct').textContent=pct!=null?Math.round(pct)+'%':'—';
  const C=2*Math.PI*64;
  $('ringVal').setAttribute('stroke-dashoffset',C*(1-(pct||0)/100));
  $('stEta').textContent=fmtETA(s.time_remaining_s);
  $('stElapsed').textContent=s.elapsed_s?fmtETA(s.elapsed_s):'—';
  const t=s.temps||{};
  $('stNoz').textContent=t.nozzle!=null?Math.round(t.nozzle)+'°'+(t.nozzle_target?' / '+Math.round(t.nozzle_target)+'°':''):'—';
  $('stBed').textContent=t.hotbed!=null?Math.round(t.hotbed)+'°'+(t.hotbed_target?' / '+Math.round(t.hotbed_target)+'°':''):'—';
  $('stCham').textContent=t.chamber!=null?Math.round(t.chamber)+'°':'—';
  $('stLayer').textContent=(s.total_layers?(s.current_layer+' / '+s.total_layers):'—');
  if(t.nozzle!=null){
    const now=Date.now()/1000;
    TEMPS.t.push(now);TEMPS.noz.push(t.nozzle);TEMPS.bed.push((t.hotbed!==undefined&&t.hotbed!==null?t.hotbed:0));
    TEMPS.nozT.push((t.nozzle_target!==undefined&&t.nozzle_target!==null?t.nozzle_target:0));TEMPS.bedT.push((t.hotbed_target!==undefined&&t.hotbed_target!==null?t.hotbed_target:0));
    while(TEMPS.t.length>360){for(const k in TEMPS)TEMPS[k].shift()}
    drawChart();
  }
  renderCmds();
}

/* ---- comandi contestuali ---- */
function renderCmds(){
  const busy=state&&(state.is_printing||state.job_active);
  const paused=state&&state.status==='paused';
  $('btnPause').disabled=!busy||paused;
  $('btnResume').disabled=!paused;
  $('btnStop').disabled=!busy;
  $('btnLight').textContent=(state&&state.light===true)?'🌑 Luce OFF':'💡 Luce ON';
}
async function cmd(kind){
  try{
    const r=await jfetch('/cmd/'+kind,{method:'POST'});
    const j=await r.json();
    if(j.ok)toast('Comando eseguito',kind,'ok',2500);
    else toast('Comando rifiutato',j.error||('codice '+j.ack),'err');
    pushEvent({ts:Date.now()/1000,type:'remote_command',data:{command:kind+' → '+(j.ok?'ok':'rifiutato')}});
  }catch(e){toast('Errore comando',String(e),'err')}
}
$('btnPause').onclick=()=>cmd('pause');
$('btnResume').onclick=()=>cmd('resume');
$('btnStop').onclick=()=>{if(confirm('Fermare davvero la stampa in corso?'))cmd('stop')};
$('btnLight').onclick=async()=>{
  const on=state?!(state.light===true):true;  // on se luce è off/null
  try{const r=await jfetch('/cmd/light',{method:'POST',body:JSON.stringify({on})});
    const j=await r.json();
    j.ok?toast('Luce interna',on?'accesa':'spenta','ok',2500):toast('Luce rifiutata',j.error||'','err');
  }catch(e){toast('Errore luce',String(e),'err')}
};
$('btnPhoto').onclick=openSnapshot;
function openSnapshot(){
  jfetch('/photo').then(r=>{if(!r.ok)throw 0;return r.blob()}).then(b=>{
    const u=URL.createObjectURL(b);
    const w=window.open('','_blank');
    if(w){w.document.write(`<title>Snapshot</title><img src="${u}" style="max-width:100%">`)}
    else toast('Apri il popup','il browser ha bloccato la finestra','info');
    setTimeout(()=>URL.revokeObjectURL(u),120000);
  }).catch(()=>toast('Foto non disponibile','','err'));
}

/* ---- SSE con riconnessione ---- */
async function sseLoop(){
  for(;;){
    try{
      const r=await jfetch('/api/events');
      if(!r.ok||!r.body)throw new Error('SSE '+r.status);
      const reader=r.body.getReader(),dec=new TextDecoder();
      let buf='';
      for(;;){
        const {done,value}=await reader.read();
        if(done)break;
        buf+=dec.decode(value,{stream:true});
        let i;
        while((i=buf.indexOf('\n\n'))>=0){
          const chunk=buf.slice(0,i);buf=buf.slice(i+2);
          for(const line of chunk.split('\n')){
            if(!line.startsWith('data: '))continue;
            try{
              const ev=JSON.parse(line.slice(6));
              if(ev.type==='state_changed')setState(ev.data||{});
              else pushEvent(ev);
            }catch(_){}
          }
        }
      }
    }catch(e){/* offline: riprova */}
    $('connChip').classList.remove('on');$('connTxt').textContent='riconnessione…';
    await new Promise(r=>setTimeout(r,3000));
  }
}

/* ============================ WEBCAM ============================ */
function initCam(){
  const img=$('cam'),wrap=$('camwrap');
  img.onload=()=>{$('liveChip').hidden=false;wrap.classList.remove('err')};
  img.onerror=()=>{$('liveChip').hidden=true;wrap.classList.add('err');
    setTimeout(()=>{img.src='/photo?ts='+Date.now()},4000)};
  img.src='/video';
  $('camSnap').onclick=openSnapshot;
  $('camFull').onclick=()=>{
    if(document.fullscreenElement)document.exitFullscreen();
    else wrap.requestFullscreen&&wrap.requestFullscreen();
  };
}

/* ============================ CHART ============================ */
function drawChart(){
  const c=$('chart');if(!c)return;
  const dpr=window.devicePixelRatio||1;
  const W=c.clientWidth*dpr,H=c.clientHeight*dpr;
  if(c.width!==W||c.height!==H){c.width=W;c.height=H}
  const x=c.getContext('2d');x.scale(dpr,dpr);
  const w=W/dpr,h=H/dpr;x.clearRect(0,0,W,H);
  if(TEMPS.t.length<2)return;
  const PAD={l:44,r:10,t:26,b:22};
  const iw=w-PAD.l-PAD.r,ih=h-PAD.t-PAD.b;
  const max=Math.max(80,...TEMPS.noz,...TEMPS.bed,...TEMPS.nozT,...TEMPS.bedT)*1.06;
  const x2px=i=>PAD.l+(i/(TEMPS.t.length-1))*iw;
  const y2px=v=>PAD.t+ih-(v/max)*ih;

  x.font='10px Inter,system-ui';x.textAlign='right';x.textBaseline='middle';
  for(let g=0;g<=5;g++){
    const v=max*g/5,y=y2px(v);
    x.strokeStyle='rgba(255,255,255,.06)';x.beginPath();x.moveTo(PAD.l,y);x.lineTo(w-PAD.r,y);x.stroke();
    x.fillStyle='#58697a';x.fillText(Math.round(v)+'°',PAD.l-6,y);
  }
  x.textAlign='center';x.textBaseline='top';
  const nLab=Math.min(6,TEMPS.t.length);
  for(let i=0;i<nLab;i++){
    const idx=Math.round(i*(TEMPS.t.length-1)/Math.max(nLab-1,1));
    x.fillStyle='#58697a';x.fillText(fmtClock(TEMPS.t[idx]),x2px(idx),h-PAD.b+6);
  }
  const line=(arr,color,dash)=>{
    x.strokeStyle=color;x.lineWidth=1.8;x.setLineDash(dash?[4,4]:[]);
    x.beginPath();
    arr.forEach((v,i)=>{const px=x2px(i),py=y2px(v);i?x.lineTo(px,py):x.moveTo(px,py)});
    x.stroke();x.setLineDash([]);
  };
  if(TEMPS.nozT.some(v=>v>0))line(TEMPS.nozT,'rgba(239,83,80,.4)',true);
  if(TEMPS.bedT.some(v=>v>0))line(TEMPS.bedT,'rgba(79,195,247,.4)',true);
  line(TEMPS.noz,'#ef5350');
  line(TEMPS.bed,'#4fc3f7');
  c._geo={PAD,iw,ih,max};
}
$('chart').addEventListener('mousemove',e=>{
  const c=$('chart'),g=c._geo;if(!g)return;
  const rect=c.getBoundingClientRect();
  const relX=(e.clientX-rect.left)-g.PAD.l;
  const i=Math.round(relX/g.iw*(TEMPS.t.length-1));
  if(i<0||i>=TEMPS.t.length){$('chartTip').style.display='none';return}
  const tip=$('chartTip');
  tip.innerHTML=`<b>${fmtClock(TEMPS.t[i])}</b>Ugello ${Math.round(TEMPS.noz[i])}° · Piatto ${Math.round(TEMPS.bed[i])}°`;
  tip.style.display='block';
  tip.style.left=Math.min(e.clientX-rect.left+12,c.clientWidth-150)+'px';
  tip.style.top=(e.clientY-rect.top-44)+'px';
});
$('chart').addEventListener('mouseleave',()=>$('chartTip').style.display='none');
window.addEventListener('resize',drawChart);

/* ============================ TABS ============================ */
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===t));
  document.querySelectorAll('.tabpage').forEach(p=>p.classList.toggle('active',p.dataset.tab===t.dataset.tab));
  if(t.dataset.tab==='file')refreshFiles();
  if(t.dataset.tab==='modelli')refreshModels();
});
function gotoTab(name){document.querySelector(`.tab[data-tab="${name}"]`).click()}

/* ============================ MODELLI ============================ */
const thumbCache={};
async function refreshModels(){
  try{
    const r=await jfetch('/models');const j=await r.json();
    const grid=$('mgrid');grid.innerHTML='';
    if(!(j.models||[]).length){
      grid.innerHTML=`<div class="empty" style="grid-column:1/-1"><div class="eico">📦</div>
        <div class="et">Nessun modello caricato</div>
        <div class="eh">Trascina un file STL / 3MF / OBJ qui sopra</div></div>`;
      return;
    }
    j.models.forEach(m=>grid.appendChild(modelCard(m)));
    j.models.filter(m=>m.name.toLowerCase().endsWith('.stl')&&m.name.length<40)
      .slice(0,8).forEach(makeThumb);
  }catch(e){}
}
function modelCard(m){
  const card=document.createElement('div');card.className='mcard';
  const isStl=m.name.toLowerCase().endsWith('.stl');
  const turl=thumbCache[m.name];
  card.innerHTML=`
    <div class="thumb" title="Apri il viewer 3D">
      ${turl?`<img src="${turl}">`:`<span class="ph">${isStl?'⏳':'📦'}</span>`}
      ${isStl?'<span class="badge info stlbadge">STL</span>':'<span class="badge dim stlbadge">'+m.name.split('.').pop().toUpperCase()+'</span>'}
    </div>
    <div class="info"><span class="nm" title="${esc(m.name)}">${esc(m.name)}</span>
      <span class="sz">${fmtMB(m.size)} · ${timeago(m.mtime)}</span></div>
    <div class="ops"></div>`;
  card.querySelector('.thumb').onclick=()=>isStl?openViewer(m.name):toast('Viewer 3D','disponibile solo per file STL','info');
  const ops=card.querySelector('.ops');
  const bS=document.createElement('button');bS.className='btn primary sm';bS.textContent='🔪 Slice';
  bS.onclick=()=>openSliceModal(m.name);ops.appendChild(bS);
  const bD=document.createElement('button');bD.className='btn ghost sm';bD.textContent='🗑';
  bD.title='Elimina';
  bD.onclick=async()=>{if(!confirm('Eliminare '+m.name+'?'))return;
    try{await jfetch('/models/'+encodeURIComponent(m.name),{method:'DELETE'});
      toast('Modello eliminato',m.name,'ok',2500);refreshModels();
    }catch(e){toast('Eliminazione fallita',String(e),'err')}};
  ops.appendChild(bD);
  return card;
}
/* ---- miniature STL via three.js offscreen ---- */
const thumbQueue=[];let thumbBusy=false;
function makeThumb(m){if(!thumbCache[m.name]&&!thumbQueue.includes(m))thumbQueue.push(m)}
function thumbLoop(){
  if(thumbBusy||!thumbQueue.length)return;
  thumbBusy=true;
  const m=thumbQueue.shift();
  jfetch('/models/'+encodeURIComponent(m.name)).then(r=>r.arrayBuffer()).then(buf=>{
    const geo=new THREE.STLLoader().parse(buf);
    geo.computeBoundingBox();geo.computeVertexNormals();
    const bb=geo.boundingBox,size=new THREE.Vector3();bb.getSize(size);
    const maxDim=Math.max(size.x,size.y,size.z)||50;
    const cnv=document.createElement('canvas');cnv.width=160;cnv.height=120;
    const rend=new THREE.WebGLRenderer({canvas:cnv,antialias:true,alpha:true});
    rend.setSize(160,120);
    const sc=new THREE.Scene();
    const mesh=new THREE.Mesh(geo,new THREE.MeshStandardMaterial({color:0x7fc4f0,metalness:.2,roughness:.55}));
    mesh.rotation.x=-Math.PI/2;sc.add(mesh);
    sc.add(new THREE.HemisphereLight(0xffffff,0x223344,1.1));
    const dl=new THREE.DirectionalLight(0xffffff,.7);dl.position.set(1,2,1);sc.add(dl);
    const cam=new THREE.PerspectiveCamera(42,160/120,.1,2000);
    const ctr=new THREE.Vector3();bb.getCenter(ctr);
    ctr.applyAxisAngle(new THREE.Vector3(1,0,0),-Math.PI/2);
    cam.position.set(ctr.x+maxDim*1.15,ctr.y+maxDim*.9,ctr.z+maxDim*1.15);
    cam.lookAt(ctr);
    rend.render(sc,cam);
    thumbCache[m.name]=cnv.toDataURL('image/jpeg',.85);
    rend.dispose();
    refreshModels();
  }).catch(()=>{}).finally(()=>{thumbBusy=false;setTimeout(thumbLoop,80)});
}
setInterval(thumbLoop,400);

/* ---- upload con progress ---- */
function uploadModel(file){
  if(!file)return;
  const fd=new FormData();fd.append('file',file);
  const bar=$('upProgress');bar.style.display='block';
  const xhr=new XMLHttpRequest();
  xhr.open('POST','/models');
  if(AUTH)xhr.setRequestHeader('Authorization','Basic '+AUTH);
  xhr.upload.onprogress=e=>{if(e.lengthComputable)bar.firstElementChild.style.width=(e.loaded/e.total*100)+'%'};
  xhr.onload=()=>{
    bar.style.display='none';bar.firstElementChild.style.width='0';
    let j={};try{j=JSON.parse(xhr.responseText)}catch(_){}
    if(xhr.status===200&&j.ok){toast('Modello caricato',file.name,'ok');refreshModels()}
    else toast('Caricamento fallita',j.detail||('HTTP '+xhr.status),'err');
  };
  xhr.onerror=()=>{bar.style.display='none';toast('Caricamento fallita','errore di rete','err')};
  xhr.send(fd);
}
const dz=$('dropzone');
dz.onclick=()=>$('modelFile').click();
$('modelFile').onchange=e=>uploadModel(e.target.files[0]);
dz.ondragover=e=>{e.preventDefault();dz.classList.add('drag')};
dz.ondragleave=()=>dz.classList.remove('drag');
dz.ondrop=e=>{e.preventDefault();dz.classList.remove('drag');uploadModel(e.dataTransfer.files[0])};

/* ============================ VIEWER 3D ============================ */
let V=null;
function openViewer(name){
  UI.openModal('viewerModal');
  $('viewerTitle').textContent=name;
  jfetch('/models/'+encodeURIComponent(name)).then(r=>r.arrayBuffer()).then(buf=>{
    if(V){V.rend.dispose();cancelAnimationFrame(V.raf)}
    const geo=new THREE.STLLoader().parse(buf);
    geo.computeBoundingBox();geo.computeVertexNormals();
    const bb=geo.boundingBox,size=new THREE.Vector3();bb.getSize(size);
    const maxDim=Math.max(size.x,size.y,size.z)||50;
    const canvas=$('stlCanvas');
    const cvw=Math.min(window.innerWidth*.92,820),cvh=Math.min(window.innerHeight*.55,520);
    canvas.width=cvw;canvas.height=cvh;canvas.style.height=cvh+'px';
    const mat=new THREE.MeshStandardMaterial({color:0x7fc4f0,metalness:.15,roughness:.55});
    const mesh=new THREE.Mesh(geo,mat);mesh.rotation.x=-Math.PI/2;
    const sc=new THREE.Scene();sc.background=new THREE.Color(0x05070a);
    const grid=new THREE.GridHelper(256,16,0x2a6a8a,0x1a2a38);sc.add(grid);sc.add(mesh);
    sc.add(new THREE.HemisphereLight(0xffffff,0x223344,1.05));
    const dl=new THREE.DirectionalLight(0xffffff,.85);dl.position.set(1,2,1);sc.add(dl);
    const cam=new THREE.PerspectiveCamera(50,cvw/cvh,.1,2000);
    const ctr=new THREE.Vector3();bb.getCenter(ctr);
    ctr.applyAxisAngle(new THREE.Vector3(1,0,0),-Math.PI/2);
    const controls=new THREE.OrbitControls(cam,canvas);
    controls.target.copy(ctr);controls.autoRotateSpeed=2;
    const reset=()=>{cam.position.set(ctr.x+maxDim*1.3,ctr.y+maxDim,ctr.z+maxDim*1.6);cam.lookAt(ctr);controls.target.copy(ctr)};
    reset();
    const rend=new THREE.WebGLRenderer({canvas,antialias:true});
    rend.setSize(cvw,cvh);
    V={rend,raf:0,wire:false,spin:false};
    $('vWire').onclick=()=>{V.wire=!V.wire;mat.wireframe=V.wire;$('vWire').classList.toggle('primary',V.wire)};
    $('vSpin').onclick=()=>{V.spin=!V.spin;controls.autoRotate=V.spin;$('vSpin').classList.toggle('primary',V.spin)};
    $('vReset').onclick=reset;
    $('vSlice').onclick=()=>{UI.closeModal('viewerModal');openSliceModal(name)};
    (function loop(){controls.update();rend.render(sc,cam);V.raf=requestAnimationFrame(loop)})();
  }).catch(e=>toast('Viewer','impossibile caricare il modello','err'));
}

/* ============================ SLICING ============================ */
let sliceProfiles={},sliceJobPoll=null,sliceModel=null;
async function loadSliceProfiles(){
  try{
    const r=await jfetch('/slice/profiles');const j=await r.json();
    sliceProfiles={};const sel=$('slProfile');sel.innerHTML='';
    (j.profiles||[]).forEach(p=>{
      sliceProfiles[p.id]=p;
      const o=document.createElement('option');o.value=p.id;
      o.textContent=p.name+(p.layer_height?' — '+p.layer_height+'mm':'');
      if(p.default)o.selected=true;
      sel.appendChild(o);
    });
    onProfileChange();
  }catch(e){}
  try{
    const r=await jfetch('/materials');const j=await r.json();
    const sel=$('slMat');sel.innerHTML='';
    (j.materials||[]).forEach(m=>{
      const o=document.createElement('option');o.value=m.id;
      o.textContent=m.name;
      sel.appendChild(o);
    });
  }catch(e){}
}
function onProfileChange(){
  const p=sliceProfiles[$('slProfile').value];
  $('slProfileHelp').textContent=p?p.description||'':'';
  if(p&&!$('slInfill').value)$('slInfill').placeholder=p.default_infill;
}
$('slProfile').addEventListener('change',onProfileChange);
function openSliceModal(name){
  sliceModel=name;
  $('sliceTitle').textContent='🔪 Slice — '+name;
  $('jobPanel').hidden=true;
  UI.openModal('sliceModal');
}
$('slGo').onclick=()=>{
  const body={profile:$('slProfile').value||'standard',
    material:$('slMat').value,
    infill:parseInt($('slInfill').value||'')||undefined,
    supports:$('slSupp').checked,
    transfer:$('slTransfer').checked};
  jfetch('/models/'+encodeURIComponent(sliceModel)+'/slice',{method:'POST',body:JSON.stringify(body)})
    .then(r=>r.json()).then(j=>{
      if(!j.ok){toast('Slice rifiutato',j.detail||'slicer non disponibile','err');return}
      $('jobPanel').hidden=false;
      trackJob(j.job.id);
    }).catch(e=>toast('Errore slice',String(e),'err'));
};
function trackJob(id){
  if(sliceJobPoll)clearInterval(sliceJobPoll);
  const upd=async()=>{
    try{
      const r=await jfetch('/slice/jobs/'+id);const j=await r.json();
      const S={queued:['in coda','warn'],running:['slicing…','info'],done:['completato','ok'],error:['errore','err']};
      const s=S[j.state]||['—','dim'];
      $('jpState').innerHTML=UI.badge(s[0],s[1]);
      $('jpTime').textContent=(j.duration_s||0)+' s';
      $('jpOut').textContent=j.state==='done'?(j.gcode||''):(j.error||'');
      $('jpLog').textContent=j.log_tail||'';
      if(j.state==='done'){
        $('jpDownload').hidden=false;$('jpPrint').hidden=false;
        if(!j._toastDone){j._toastDone=true;toast('Slice completato',j.gcode,'ok')}
      }else{$('jpDownload').hidden=true;$('jpPrint').hidden=true}
      if(j.state==='error'&&!j._toastErr){j._toastErr=true;toast('Slice fallito',String(j.error||'').slice(0,90),'err')}
    }catch(_){}
  };
  upd();sliceJobPoll=setInterval(upd,2000);
}
$('jpLogBtn').onclick=()=>{const l=$('jpLog');l.style.display=l.style.display==='block'?'none':'block'};
$('jpDownload').onclick=()=>{
  const g=$('jpOut').textContent;if(!g)return;
  jfetch('/models/../gcodes/'+encodeURIComponent(g)).catch(()=>{});
  window.open('/files','_blank');
  toast('Download','usa la scheda File GCODE per scaricare: '+g,'info',6000);
};
$('jpPrint').onclick=async()=>{
  const g=$('jpOut').textContent;if(!g)return;
  try{
    const r=await jfetch('/print',{method:'POST',body:JSON.stringify({filename:g})});
    const j=await r.json();
    if(j.ok){toast('Stampa avviata',g,'ok');UI.closeModal('sliceModal');gotoTab('eventi')}
    else toast('Avvio rifiutato',j.error||j.detail||('codice '+(j.ack!=null?j.ack:'?')),'err');
  }catch(e){toast('Errore avvio',String(e),'err')}
};

/* ============================ FILE GCODE ============================ */
async function refreshFiles(){
  try{
    const r=await jfetch('/files');const j=await r.json();
    const loc=$('localFiles'),prn=$('printerFiles');
    loc.innerHTML='';prn.innerHTML='';
    (j.local||[]).forEach(f=>loc.appendChild(fileRow(f.name,f.size,f.mtime,true)));
    if(!(j.local||[]).length)loc.innerHTML=emptyRow(' Nessun GCODE locale');
    if(j.printer==null){prn.innerHTML=emptyRow('Stampante non connessa')}
    else{
      (j.printer||[]).slice(0,40).forEach(f=>{
        const nm=String(f.name||'').replace(/^\/local\/+/,'');
        if(nm)prn.appendChild(fileRow(nm,f.usedSize,null,false));
      });
      if(!(j.printer||[]).length)prn.innerHTML=emptyRow('Nessun file sulla stampante');
    }
  }catch(e){}
}
function emptyRow(txt){return `<div class="empty" style="padding:18px"><div class="et">${esc(txt)}</div></div>`}
function fileRow(name,size,mtime,local){
  const row=document.createElement('div');row.className='filerow';
  const short=name.length>44?name.slice(0,41)+'…':name;
  row.innerHTML=`<div class="ico">${local?'💽':'🖨️'}</div>
    <div class="fi"><div class="nm" title="${esc(name)}">${esc(short)}</div>
    <div class="mt">${size?fmtMB(size):''}${mtime?' · '+timeago(mtime/1000):''}${local?'':' · sulla stampante'}</div></div>
    <div class="ops"></div>`;
  const ops=row.querySelector('.ops');
  const bP=document.createElement('button');bP.className='btn primary sm';bP.textContent='🖨 Stampa';
  bP.onclick=async()=>{
    try{const r=await jfetch('/print',{method:'POST',body:JSON.stringify({filename:name})});
      const j=await r.json();
      j.ok?toast('Stampa avviata',name,'ok'):toast('Avvio rifiutato',j.error||('codice '+(j.ack!=null?j.ack:'?')),'err');
    }catch(e){toast('Errore',String(e),'err')}
  };
  ops.appendChild(bP);
  return row;
}

/* ============================ EVENTI ============================ */
const EV_META={
  print_started:['▶️','Stampe'],print_completed:['✅','Stampe'],print_failed:['❌','Stampe'],
  print_paused:['⏸️','Stampe'],print_resumed:['▶️','Stampe'],printer_error:['⚠️','Sistema'],
  ai_alert:['🤖','AI'],remote_command:['🎛️','Comandi'],printer_disconnected:['🔌','Sistema'],
  printer_connected:['🔗','Sistema'],print_progress:['📊','Stampe'],temperature_update:['🌡️','Sistema'],
};
const EVCLS={Stampe:'print',AI:'ai',Comandi:'cmd',Sistema:'sys'};
function evCat(t){return (EV_META[t]||['•','Sistema'])[1]}
function renderEvents(){
  const box=$('evList');box.innerHTML='';
  const list=EVENTS.filter(e=>evFilter==='all'||evCat(e.type)===evFilter).slice(0,60);
  if(!list.length){box.innerHTML=`<div class="empty"><div class="eico">📡</div>
    <div class="et">Nessun evento${evFilter!=='all'?' in questo filtro':''}</div></div>`;return}
  list.forEach(ev=>{
    const meta=EV_META[ev.type]||['•','Sistema'];
    const d=ev.data||{};
    let extra='';
    if(ev.type==='ai_alert')extra=` · ${d.type||''} ${d.severity||''}`;
    if(ev.type==='remote_command')extra=` · ${d.command||''}${d.value!==undefined?'='+d.value:''}`;
    const hasDetail=JSON.stringify(d)!=='{}';
    const row=document.createElement('div');row.className='evrow'+(hasDetail?' clickable':'');
    row.innerHTML=`<div class="eico">${meta[0]}</div>
      <div class="evbody"><div class="evtitle">${esc(ev.type)}${extra?`<span class="extra">${esc(extra)}</span>`:''}
        ${hasDetail?'<div class="evdetail"></div>':''}</div></div>
      <span class="evtime">${timeago(ev.ts)}</span>`;
    if(hasDetail){
      row.querySelector('.evdetail').textContent=JSON.stringify(d,null,1);
      row.onclick=()=>row.querySelector('.evdetail').classList.toggle('open');
    }
    box.appendChild(row);
  });
}
document.querySelectorAll('#evChips .chip').forEach(c=>c.onclick=()=>{
  document.querySelectorAll('#evChips .chip').forEach(x=>x.classList.toggle('active',x===c));
  evFilter=c.dataset.f;renderEvents();
});

/* ============================ AI PANEL ============================ */
async function refreshAI(){
  try{
    const r=await jfetch('/ai/metrics');const m=await r.json();
    if(!m||m.enabled===false){$('aiMlVal').textContent='disattiva';return}
    const ml=m.ml||{};
    const mm=m.last_metrics||{};
    if(ml.enabled===false){$('aiMlVal').textContent='modello assente';}
    else{
      const score=(ml.score!==undefined&&ml.score!==null?ml.score:0);
      const pct=Math.round(score*100);
      $('aiMlBar').style.width=pct+'%';
      $('aiMlBar').style.background=pct>75?'var(--err)':pct>45?'var(--warn)':'var(--ok)';
      $('aiMlVal').textContent=score.toFixed(3)+' ('+ml.prediction+')' + (score>ml.threshold?' ⚠':'');
    }
    const sev=(mm.spaghetti_severity!==undefined&&mm.spaghetti_severity!==null?mm.spaghetti_severity:0),base=(mm.spaghetti_baseline!==undefined&&mm.spaghetti_baseline!==null?mm.spaghetti_baseline:0);
    const cvpct=Math.min(sev/(mm.spaghetti_threshold||.01)*100,100);
    $('aiCvBar').style.width=cvpct+'%';
    $('aiCvBar').style.background=cvpct>100?'var(--err)':cvpct>60?'var(--warn)':'var(--ok)';
    $('aiCvVal').textContent=sev.toFixed(4)+' / base '+base.toFixed(4);
    const lw=m.layer_watch;
    $('aiLwVal').textContent=lw?('layer '+(lw.last_layer!=null?lw.last_layer:'—')+' · '+((lw.samples!==undefined&&lw.samples!==null?lw.samples:0))+' campioni'):'—';
    const hist=(lw&&lw.history)||[];
    const bars=document.querySelectorAll('#aiHist .bar');
    bars.forEach((b,i)=>{
      const h=hist[hist.length-bars.length+i];
      const score=h?h.score:0;
      b.style.height=Math.max(4,Math.min(score*100,100))+'%';
      b.className='bar'+(score>1?' crit':score>0.4?' warn':' ok');
      b.title=h?('layer '+h.layer+': '+h.score):'';
    });
    refreshSnaps();
  }catch(_){}
}
async function refreshSnaps(){
  try{
    const r=await jfetch('/snapshots');const j=await r.json();
    const box=$('aiSnaps');
    (j.snapshots||[]).slice(0,6).forEach(s=>{
      if(box.querySelector(`[data-n="${s.name}"]`))return;
      const d=document.createElement('div');d.className='snap';d.dataset.n=s.name;
      d.innerHTML=`<img loading="lazy" src="/snapshots/${encodeURIComponent(s.name)}" alt="${esc(s.name)}">
        <div class="cap">${esc(s.name.replace(/\.jpg$/,'').replace(/^\d{8}_\d{6}/,''))}</div>`;
      d.querySelector('img').onclick=e=>window.open(e.target.src,'_blank');
      box.prepend(d);
    });
    while(box.children.length>12)box.lastChild.remove();
  }catch(_){}
}
$('aiRefresh').onclick=()=>{refreshAI();toast('AI','metriche aggiornate','info',2000)};
setInterval(refreshAI,4000);

/* ============================ BOOT ============================ */
function boot(){
  jfetch('/status').then(r=>r.json()).then(setState).catch(()=>{});
  refreshModels();
  loadSliceProfiles();
  refreshAI();
}

/* ============================ MATERIALI & PROFILI (CRUD) ============================ */
let editingMat=null, materialsList=[], profilesList=[];

async function refreshProfiles(){
  await Promise.all([refreshMaterials(), refreshPrintProfiles()]);
}

async function refreshMaterials(){
  try{
    const r=await jfetch('/materials');const j=await r.json();
    materialsList=j.materials||[];
    const grid=$('matGrid');grid.innerHTML='';
    materialsList.forEach(m=>{
      const c=document.createElement('div');c.className='card';c.style.margin='0';
      const badge=m.builtin?'<span class="badge dim">built-in</span>':'<span class="badge ok">custom</span>';
      c.innerHTML=`<h2><span style="display:inline-block;width:14px;height:14px;border-radius:4px;background:${m.color||'#888'};margin-right:6px;vertical-align:-2px"></span>${esc(m.name)} ${badge}</h2>
        <div class="pfield"><span>Ugello</span><b>${m.nozzle_temperature}°C</b></div>
        <div class="pfield"><span>Piatto</span><b>${m.bed_temperature}°C</b></div>
        <div class="pfield"><span>Ventola</span><b>${m.fan_min_speed}-${m.fan_max_speed}%</b></div>
        <div class="pfield"><span>VMS</span><b>${m.filament_max_volumetric_speed} mm³/s</b></div>
        <div style="font-size:.78rem;color:var(--txt-dim);margin-top:8px">${esc(m.description||'')}</div>
        <button class="btn sm" style="margin-top:10px" data-mid="${m.id}">✏️ Modifica</button>`;
      c.querySelector('button').onclick=()=>openMatEditor(m);
      grid.appendChild(c);
    });
    if(!materialsList.length)grid.innerHTML='<div class="empty"><div class="et">Nessun materiale</div></div>';
  }catch(e){}
}

async function refreshPrintProfiles(){
  try{
    const r=await jfetch('/profiles');const j=await r.json();
    profilesList=j.profiles||[];
    const grid=$('profilesGrid');grid.innerHTML='';
    profilesList.forEach(p=>{
      const c=document.createElement('div');c.className='card';c.style.margin='0';
      const badge=p.builtin?'<span class="badge dim">built-in</span>':'<span class="badge ok">custom</span>';
      c.innerHTML=`<h2>${esc(p.name)} ${badge}</h2>
        <div class="pfield"><span>Layer</span><b>${p.layer_height||'—'} mm</b></div>
        <div class="pfield"><span>Pareti</span><b>${p.perimeters||p.walls||'—'}</b></div>
        <div class="pfield"><span>Infill def.</span><b>${p.default_infill||'—'}%</b></div>
        <div style="font-size:.78rem;color:var(--txt-dim);margin-top:8px">${esc(p.description||'')}</div>
        ${!p.builtin?'<button class="btn sm" style="margin-top:10px" data-pid="'+p.id+'">✏️ Modifica</button>':''}`;
      if(!p.builtin)c.querySelector('button').onclick=()=>openProfEditor(p);
      grid.appendChild(c);
    });
  }catch(e){}
}

const MAT_FIELDS=[
  ['name','Nome','text','PLA Basic'],
  ['filament_type','Tipo filamento','text','PLA'],
  ['nozzle_temperature','Ugello °C','number',210],
  ['nozzle_temperature_initial_layer','Ugello 1° layer °C','number',210],
  ['bed_temperature','Piatto °C','number',60],
  ['bed_temperature_initial_layer','Piatto 1° layer °C','number',60],
  ['fan_min_speed','Ventola min %','number',100],
  ['fan_max_speed','Ventola max %','number',100],
  ['filament_max_volumetric_speed','VMS mm³/s','number',15],
  ['filament_flow_ratio','Flow ratio','number',0.98],
  ['filament_density','Densità g/cm³','number',1.25],
  ['retraction_length','Retrazione mm','number',0.8],
  ['retraction_speed','Vel. ritrazione mm/s','number',40],
  ['color','Colore','text','#cccccc'],
  ['description','Descrizione','text',''],
];

function openMatEditor(m){
  editingMat=m?{...m}:{id:'custom_'+Date.now(),name:'',filament_type:'',
    nozzle_temperature:210,nozzle_temperature_initial_layer:210,
    bed_temperature:60,bed_temperature_initial_layer:60,
    fan_min_speed:100,fan_max_speed:100,filament_max_volumetric_speed:15,
    filament_flow_ratio:0.98,filament_density:1.25,retraction_length:0.8,
    retraction_speed:40,color:'#cccccc',description:''};
  $('matModalTitle').textContent=m?('✏️ '+m.name):'➕ Nuovo materiale';
  $('matDelete').hidden=!m||m.builtin===true;
  const body=$('matModalBody');body.innerHTML='';
  MAT_FIELDS.forEach(([key,label,type,def])=>{
    const l=document.createElement('label');l.className='field';
    l.innerHTML=`<span>${label}</span>`;
    const inp=document.createElement('input');
    inp.type=type;inp.id='mf_'+key;inp.value=editingMat[key]!==undefined?editingMat[key]:def;
    if(type==='number'){inp.step='0.01';inp.min='0'}
    l.appendChild(inp);body.appendChild(l);
  });
  UI.openModal('matModal');
}

$('matSave').onclick=async()=>{
  const data={...editingMat};
  MAT_FIELDS.forEach(([key])=>{
    const el=$('mf_'+key);if(!el)return;
    data[key]=el.type==='number'?parseFloat(el.value)||0:el.value;
  });
  try{
    const r=await jfetch('/materials/'+encodeURIComponent(data.id),{method:'PUT',body:JSON.stringify(data)});
    const j=await r.json();
    if(j.ok!==undefined){toast('Materiale salvato',data.name,'ok');UI.closeModal('matModal');refreshMaterials()}
    else toast('Errore',j.detail||'','err');
  }catch(e){toast('Errore',String(e),'err')}
};

$('matDelete').onclick=async()=>{
  if(!confirm('Eliminare '+editingMat.name+'?'))return;
  try{await jfetch('/materials/'+encodeURIComponent(editingMat.id),{method:'DELETE'});
    toast('Materiale eliminato',editingMat.name,'ok');UI.closeModal('matModal');refreshMaterials();
  }catch(e){toast('Errore',String(e),'err')}
};

$('matAdd').onclick=()=>openMatEditor(null);

// profili di stampa custom
const PROF_FIELDS=[
  ['name','Nome','text','Il mio profilo'],
  ['layer_height','Layer (mm)','number',0.2],
  ['perimeters','Pareti','number',2],
  ['top_solid_layers','Layer top','number',5],
  ['bottom_solid_layers','Layer bottom','number',3],
  ['external_perimeter_speed','Vel. parete esterna mm/s','number',160],
  ['perimeter_speed','Vel. pareti interne mm/s','number',200],
  ['infill_speed','Vel. riempimento mm/s','number',200],
  ['default_infill','Infill default %','number',15],
  ['description','Descrizione','text',''],
];

function openProfEditor(p){
  const data={id:p? p.id : 'custom_'+Date.now(),name:'',layer_height:0.2,perimeters:2,
    top_solid_layers:5,bottom_solid_layers:3,external_perimeter_speed:160,
    perimeter_speed:200,infill_speed:200,default_infill:15,description:'',...(p||{})};
  $('matModalTitle').textContent=p?('✏️ '+p.name):'➕ Nuovo profilo di stampa';
  $('matDelete').hidden=!p||p.builtin===true;
  const body=$('matModalBody');body.innerHTML='';
  PROF_FIELDS.forEach(([key,label,type,def])=>{
    const l=document.createElement('label');l.className='field';
    l.innerHTML=`<span>${label}</span>`;
    const inp=document.createElement('input');
    inp.type=type;inp.id='mf_'+key;inp.value=data[key]!==undefined?data[key]:def;
    if(type==='number'){inp.step='0.01'}
    l.appendChild(inp);body.appendChild(l);
  });
  editingMat=data;
  UI.openModal('matModal');
}

// override save per profili
const _origMatSave=$('matSave').onclick;
$('matSave').onclick=async()=>{
  if(editingMat && editingMat.id && editingMat.id.startsWith('custom_prof_')){
    // è un profilo di stampa
    const data={...editingMat};
    PROF_FIELDS.forEach(([key])=>{
      const el=$('mf_'+key);if(!el)return;
      data[key]=el.type==='number'?parseFloat(el.value)||0:el.value;
    });
    try{
      const r=await jfetch('/profiles/'+encodeURIComponent(data.id),{method:'PUT',body:JSON.stringify(data)});
      const j=await r.json();
      if(j.ok!==undefined){toast('Profilo salvato',data.name,'ok');UI.closeModal('matModal');refreshPrintProfiles()}
      else toast('Errore',j.detail||'','err');
    }catch(e){toast('Errore',String(e),'err')}
    return;
  }
  _origMatSave();
};

$('profAdd').onclick=()=>openProfEditor(null);

/* ============================ FILAMENTO ============================ */
async function refreshFilament(){
  try{
    const r=await jfetch('/filament');const j=await r.json();
    // bobina attiva
    const act=$('activeSpool');
    const activeId=j.active;
    const active=(j.spools||[]).find(s=>s.id===activeId);
    if(active){
      const pct=active.remaining_g/active.weight_g*100;
      act.innerHTML=`<div class="spool-active">
        <div class="spool-info"><b>${esc(active.name)}</b>
          <span>${active.material.toUpperCase()} · ${esc(active.color)}</span></div>
        <div class="gauge" style="margin-top:8px"><div style="width:${pct}%;background:${pct<15?'var(--err)':pct<40?'var(--warn)':'var(--ok)'}"></div></div>
        <div style="display:flex;justify-content:space-between;margin-top:6px;font-size:.8rem;color:var(--txt-dim)">
          <span>Restano <b style="color:var(--txt)">${Math.round(active.remaining_g)} g</b></span>
          <span>di ${active.weight_g} g</span></div>
        ${pct<15?'<div class="badge err" style="margin-top:8px">⚠ Quasi esaurita</div>':''}
      </div>`;
    }else{
      act.innerHTML='<div class="empty"><div class="et">Nessuna bobina attiva</div><div class="eh">Seleziona o aggiungi una bobina qui sotto</div></div>';
    }
    // lista bobine
    const list=$('spoolList');list.innerHTML='';
    (j.spools||[]).forEach(s=>{
      const row=document.createElement('div');row.className='filerow';
      const pct=s.remaining_g/s.weight_g*100;
      const short=s.name.length>30?s.name.slice(0,27)+'…':s.name;
      row.innerHTML=`<div class="ico">${s.id===activeId?'🟢':'⭕'}</div>
        <div class="fi"><div class="nm">${esc(short)}</div>
        <div class="mt">${s.material.toUpperCase()} · ${esc(s.color)} · ${Math.round(s.remaining_g)}/${s.weight_g}g</div></div>
        <div class="ops"></div>`;
      const ops=row.querySelector('.ops');
      if(s.id!==activeId){
        const bA=document.createElement('button');bA.className='btn sm';bA.textContent='Attiva';
        bA.onclick=async()=>{await jfetch('/filament/active',{method:'POST',body:JSON.stringify({id:s.id})});
          toast('Bobina attivata',s.name,'ok',2500);refreshFilament()};
        ops.appendChild(bA);
      }
      const bD=document.createElement('button');bD.className='btn ghost sm';bD.textContent='🗑';
      bD.onclick=async()=>{if(!confirm('Eliminare '+s.name+'?'))return;
        await jfetch('/filament/spool/'+s.id,{method:'DELETE'});
        toast('Bobina eliminata',s.name,'ok',2500);refreshFilament()};
      ops.appendChild(bD);
      list.appendChild(row);
    });
    if(!(j.spools||[]).length)list.innerHTML='<div class="empty" style="padding:18px"><div class="et">Nessuna bobina in inventario</div></div>';
  }catch(e){}
}
$('spAdd').onclick=async()=>{
  const body={material:$('spMat').value,color:$('spColor').value||'neutro',
    weight_g:parseFloat($('spWeight').value)||1000,name:$('spName').value};
  try{const r=await jfetch('/filament/spool',{method:'POST',body:JSON.stringify(body)});
    const j=await r.json();
    if(j.ok){toast('Bobina aggiunta',j.spool.name,'ok');refreshFilament()}
    else toast('Errore',j.detail||'','err');
  }catch(e){toast('Errore',String(e),'err')}
};

/* ============================ STATISTICHE ============================ */
async function refreshStats(){
  try{
    const r=await jfetch('/stats');const j=await r.json();
    const cards=$('statCards');cards.innerHTML='';
    const mk=(label,val,sub,icon)=>{const d=document.createElement('div');d.className='card';
      d.innerHTML=`<h2>${icon} ${label}</h2><div class="big" style="font-size:1.8rem">${val}</div>
        <div style="font-size:.78rem;color:var(--txt-faint)">${sub}</div>`;return d};
    cards.appendChild(mk('Stampe',j.total_prints||0,(j.successful||0)+' ok · '+(j.failed||0)+' fallite','🖨️'));
    cards.appendChild(mk('Successo',(j.success_rate!=null?j.success_rate:0)+'%',j.total_prints>0?'':'nessun dato','✅'));
    cards.appendChild(mk('Filamento',(j.total_filament_m||0)+' m',Math.round(j.total_filament_g||0)+' g totali','🧵'));
    cards.appendChild(mk('Tempo stampa',(j.total_print_time_h||0)+' h','ore di stampa cumulate','⏱️'));
    // per materiale
    const mat=$('statMaterial');mat.innerHTML='';
    const entries=Object.entries(j.by_material||{});
    if(!entries.length){mat.innerHTML='<div class="empty" style="padding:14px"><div class="et">Nessun dato</div></div>';return}
    const maxC=Math.max(...entries.map(e=>e[1]));
    entries.forEach(([m,c])=>{
      const row=document.createElement('div');row.style.cssText='display:flex;align-items:center;gap:12px;padding:6px 0';
      row.innerHTML=`<span style="width:60px;font-weight:600;text-transform:uppercase">${esc(m)}</span>
        <div class="gauge" style="flex:1"><div style="width:${c/maxC*100}%;background:var(--accent)"></div></div>
        <span style="width:30px;text-align:right;font-size:.85rem">${c}</span>`;
      mat.appendChild(row);
    });
    // recenti
    const rec=$('statRecent');rec.innerHTML='';
    (j.recent||[]).reverse().forEach(p=>{
      const row=document.createElement('div');row.className='filerow';
      const d=new Date((p.ts||0)*1000);
      const short=p.filename&&p.filename.length>36?p.filename.slice(0,33)+'…':(p.filename||'?');
      row.innerHTML=`<div class="ico">${p.success?'✅':'❌'}</div>
        <div class="fi"><div class="nm">${esc(short)}</div>
        <div class="mt">${d.toLocaleDateString('it-IT')} · ${fmtETA(p.duration_s)} · ${(p.filament_mm/1000).toFixed(1)}m ${p.material.toUpperCase()}</div></div>`;
      rec.appendChild(row);
    });
    if(!(j.recent||[]).length)rec.innerHTML='<div class="empty" style="padding:14px"><div class="et">Nessuna stampa registrata</div></div>';
  }catch(e){}
}

/* ---- hook nei tab ---- */
const _origTab=document.querySelectorAll('.tab').forEach;
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
  const name=t.dataset.tab;
  if(name==='profili')refreshProfiles();
  if(name==='filamento')refreshFilament();
  if(name==='stats')refreshStats();
}));

initCam();
boot();
sseLoop();

