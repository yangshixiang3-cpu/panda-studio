// 画布：深色无限画布，只做生图（交互参考 LibTV 画布，精简版）。
// - 节点是图片卡片；点一下节点，下面弹出输入面板（上传、画布参考、提示词预设、提示词、模型、比例 · 画质 · 尺寸 · 张数）。
//   比例、画质、尺寸、张数设一次就记住，新节点都按这个。一次生成多张时，每张一个节点，在原节点下面纵向排开。
// - 鼠标：空白处是箭头，拖动框选；节点上是小抓手，按住左键拖动；按住中键拖动移动画布；滚轮缩放。
// - 拖动节点时像 PS / PPT 一样对齐别的节点（边、中线），显示虚线；没对上别的节点的方向对齐格栅点。按住 Alt 自由移动。
// - 选中多个节点可以「打组」，组有外框，拖外框整组移动；从组框右边的「+」拉线到生图节点，整组图都当参考图。
// - 提示词里输入 @ 引用参考图，生成时按参考图顺序换成「图1」「图2」。
// - 顶栏有项目和多张画布（web/canvas-projects.js）。生图接口按「渠道」存好几套（web/canvas-channels.js），面板里点模型切换。
// 节点、连线的底子用开源的 Drawflow（MIT，web/vendor/drawflow），拖动、框选、缩放、外观在这里改写。
import Drawflow from './vendor/drawflow/drawflow.js';
import {openLightbox} from './lightbox.js';
import {copyImage} from './clipboard.js';
import {RATIOS,SIZES,QUALITIES,NODE_WIDTH,GRID,ALIGN_THRESHOLD,clampZoom,snap,snapPoint,toWorld,zoomAround,fitView,mediaHeight,
  stackBelow,rowLayout,besideOf,paramsText,nodeName,runTime,alignGuides,boundsOf,groupFrame,curvePath,resolveMentions,progressAt,branchLayout,
  canvasPollDelay,MAX_CANVAS_POLL_FAILURES} from './canvas-model.js';
import {BUILTIN_PRESETS,appendPrompt} from './canvas-presets.js';
import {installCanvasProjects} from './canvas-projects.js';
import {installChannels} from './canvas-channels.js';

const SVG_NS='http://www.w3.org/2000/svg';
const icon=d=>`<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
const ICON={
  image:icon('<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="m4 16 4.5-4.5a1.5 1.5 0 0 1 2.1 0L17 18"/><circle cx="15" cy="9" r="1.6"/>'),
  next:icon('<path d="M5 12h12m-5-5 5 5-5 5"/>'),
  regen:icon('<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>'),
  expand:icon('<path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/>'),
  folder:icon('<path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2.5h7a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>'),
  trash:icon('<path d="M4.5 7h15M9.5 7V5h5v2m-8 0 1 12.5h9L17.5 7"/>'),
  model:icon('<circle cx="12" cy="12" r="7.5"/><path d="M12 4.5v15M4.5 12h15"/>'),
  frame:icon('<rect x="4" y="7" width="16" height="10" rx="2"/>'),
  down:icon('<path d="m7 10 5 5 5-5"/>'),
  bolt:icon('<path d="M13 3 5.5 13.5H11L10 21l8.5-11H13z"/>'),
  close:icon('<path d="M7 7l10 10M17 7 7 17"/>'),
  group:icon('<rect x="3.5" y="3.5" width="17" height="17" rx="3" stroke-dasharray="3 3"/><rect x="7" y="7" width="5" height="5" rx="1"/><rect x="12" y="12" width="5" height="5" rx="1"/>'),
  check:icon('<path d="m5 12.5 4.5 4.5L19 7"/>'),
  copy:icon('<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>'),
  ungroup:icon('<rect x="3.5" y="3.5" width="8" height="8" rx="1.5"/><rect x="12.5" y="12.5" width="8" height="8" rx="1.5"/>'),
};
const CLASS={gen:'cv-node cv-gen-node',upload:'cv-node cv-upload-node'};
// 边缘流光（生图中的节点、空画布的开场卡片都用）：一层彩色锥形渐变在转，只露出四周一圈往中间渐隐，
// 外加一圈更亮的细边。只用 transform 转，不重画，所以动起来顺
const FLOWLIGHT='<div class="cv-flowlight" aria-hidden="true"><span class="wide"><i></i></span><span class="rim"><i></i></span></div>';
// 比例图标：在 24×24 里画一个按比例的圆角框（长边 14）
function ratioIcon(ratio){
  const [w,h]=String(ratio||'16:9').split(':').map(Number),k=14/Math.max(w,h),rw=w*k,rh=h*k;
  return icon(`<rect x="${(12-rw/2).toFixed(1)}" y="${(12-rh/2).toFixed(1)}" width="${rw.toFixed(1)}" height="${rh.toFixed(1)}" rx="1.8"/>`);
}
// 每个渠道生一张图大概要多久（秒），用来估进度；存在这台电脑上，越用越准
const DURATIONS_KEY='cv-gen-seconds';
function readDurations(){try{return JSON.parse(localStorage.getItem(DURATIONS_KEY)||'{}')||{};}catch{return {};}}
function learnDuration(channel,seconds){
  if(!channel||!(seconds>1&&seconds<1800))return;
  const all=readDurations(),old=Number(all[channel]);
  all[channel]=Math.round(old>0?old*0.6+seconds*0.4:seconds);
  try{localStorage.setItem(DURATIONS_KEY,JSON.stringify(all));}catch{}
}
const EMPTY_TARGETS=['parent-drawflow','drawflow'];
const isEmpty=target=>EMPTY_TARGETS.includes(target?.classList?.[0]);

// Drawflow 子类：
// - 左键在空白处：框选（不再平移）；中键：移动画布；右键：不管
// - 左键在节点上：交给画布自己拖（要对齐参考线、多选一起拖），Drawflow 只负责选中
// - 滚轮以鼠标为中心缩放；Delete 交给画布（可撤销、可多选）
class Board extends Drawflow {
  click(e){
    if(e.button===2)return;
    if(e.button===1){
      e.preventDefault();                        // 不出 Windows 的中键自动滚动
      this.editor_selected=true;this.pos_x=e.clientX;this.pos_y=e.clientY;
      this.container.classList.add('cv-panning');
      return;
    }
    const empty=isEmpty(e.target);
    super.click(e);
    if(empty){this.editor_selected=false;this.dispatch('marquee',e);return;}
    if(this.drag){
      this.drag=false;
      this.dispatch('nodePress',{id:this.ele_selected.id.slice(5),event:e,still:!!e.target.closest?.('.cv-nodrag')});
    }
  }
  dragEnd(e){super.dragEnd(e);this.container.classList.remove('cv-panning');}
  zoom_enter(event){
    event.preventDefault();
    const speed=event.ctrlKey?0.01:0.0015;              // 触控板捏合带 ctrlKey，步子大一些
    this.zoomTo(this.zoom*Math.exp(-event.deltaY*speed),event.clientX,event.clientY);
  }
  zoomTo(zoom,clientX,clientY){
    const rect=this.container.getBoundingClientRect();
    if(clientX===undefined){clientX=rect.left+rect.width/2;clientY=rect.top+rect.height/2;}
    this.setView(zoomAround({x:this.canvas_x,y:this.canvas_y,zoom:this.zoom},{x:rect.left,y:rect.top},clientX,clientY,zoom));
  }
  setView({x,y,zoom}){
    this.canvas_x=x;this.canvas_y=y;this.zoom=clampZoom(zoom);this.zoom_last_value=this.zoom;
    this.precanvas.style.transform=`translate(${x}px, ${y}px) scale(${this.zoom})`;
    this.dispatch('zoom',this.zoom);this.dispatch('translate',{x,y});
  }
  zoom_refresh(){this.setView({x:this.canvas_x,y:this.canvas_y,zoom:this.zoom});}
  contextmenu(e){e.preventDefault();}
  key(e){
    if(/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName))return;
    if(e.key==='Delete'||e.key==='Backspace'){
      e.preventDefault();
      if(this.connection_selected){this.removeConnection();return;}
      this.dispatch('deleteKey');return;
    }
    this.dispatch('keydown',e);
  }
}

export function installCanvas({$,api,toast,toastAction,guard,escape}){
  const host=$('canvas-stage'),wrap=host.parentElement,panel=$('cv-panel'),pop=$('cv-pop');
  let board=null,ready=false,nodes=new Map(),groups=[],seq=0,saveTimer,viewTimer,pollTimer,tickTimer,prefsTimer;
  let settings=null,prefs={gen:{ratio:'16:9',quality:'standard',resolution:'2K',count:1},presets:[]};
  let pid=null,cid=null,pendingConnection=null,lastDrop=null,panelId=null,popKind=null;
  let steps=[],historyAt=-1,historySerial=0,commitTimer=null,batching=0,highWater=1;const genCache=new Map();
  let picked=new Set(),pickedGroup=null,revealing=false,pickedLines=new Set(),mentionState=null,viewAnim=0;
  const projectsUI=installCanvasProjects({$,api,toast,toastAction,guard,escape,open:openCanvas});
  const channelsUI=installChannels({$,api,toast,guard,escape,onChange:next=>{settings=next;settingsPill();}});

  // ── 保存 ──
  const data=()=>board.drawflow.drawflow.Home.data;
  const boardUrl=()=>`/api/canvas/projects/${pid}/canvases/${cid}/board`;
  function edges(){
    const list=[];
    for(const [id,entry] of Object.entries(data()))
      for(const link of entry.outputs.output_1?.connections||[])list.push({from:id,to:String(link.node)});
    return list;
  }
  function snapshot(){
    return {view:{x:board.canvas_x,y:board.canvas_y,zoom:board.zoom},seq,groups,
      nodes:[...nodes.values()].map(n=>({...n,x:data()[n.id]?.pos_x??0,y:data()[n.id]?.pos_y??0})),edges:edges()};
  }
  function persist(delay=500){
    $('canvas-empty').hidden=nodes.size>0;
    clearTimeout(saveTimer);
    if(!pid)return;
    const url=boardUrl();
    saveTimer=setTimeout(()=>{saveTimer=null;api(url,snapshot(),'PUT').catch(error=>toast('画布没保存上：'+error.message));},delay);
  }
  // 存盘，同时记一步撤销。改提示词（delay≥800，边打边存）停下来才记一步，不会一个字一步
  function save(delay=500){
    persist(delay);
    clearTimeout(commitTimer);commitTimer=null;
    if(delay>=800)commitTimer=setTimeout(commit,delay);else commit();
  }

  // ── 撤销 / 重做（Ctrl+Z、Ctrl+Shift+Z / Ctrl+Y，最多 60 步）──
  // 每次改动后记一份画布：节点、位置、连线、分组、提示词和设置。
  // 生成出来的图和生成状态不算改动：撤销不会把已经出的图撤掉，节点还在就一直显示它最新的图。
  const HISTORY_LIMIT=60,GEN_FIELDS=['results','current','run'];
  function capture(){
    const list=[...nodes.values()].map(n=>({...structuredClone(n),x:data()[n.id]?.pos_x??0,y:data()[n.id]?.pos_y??0}));
    for(const n of list)if(n.kind==='gen')genCache.set(n.id,Object.fromEntries(GEN_FIELDS.map(k=>[k,n[k]])));
    const shape=list.map(n=>n.kind==='gen'?Object.fromEntries(Object.entries(n).filter(([k])=>!GEN_FIELDS.includes(k))):n);
    return {key:JSON.stringify({groups,edges:edges(),nodes:shape}),seq,groups:structuredClone(groups),nodes:list,edges:edges()};
  }
  function commit(){
    clearTimeout(commitTimer);commitTimer=null;
    if(!board||!pid||batching)return;
    const state=capture(),top=steps[historyAt];
    if(top?.key===state.key){steps[historyAt]={...state,n:top.n};return;}    // 只是出了图、挪了视角：不算一步
    steps=steps.slice(0,historyAt+1);steps.push({...state,n:++historySerial});
    if(steps.length>HISTORY_LIMIT)steps.shift();
    historyAt=steps.length-1;undoButtons();
  }
  // 好几步合成一步撤销（分出脑图、粘贴、删除……）
  function batch(fn){
    batching+=1;
    try{return fn();}finally{batching-=1;if(!batching)commit();}
  }
  function undoButtons(){
    $('cv-undo').disabled=historyAt<=0;$('cv-redo').disabled=historyAt<0||historyAt>=steps.length-1;
  }
  function undo(){
    commit();
    if(historyAt<=0){toast('没有可以撤销的了');return;}
    historyAt-=1;applyState(steps[historyAt]);
  }
  function redo(){
    commit();
    if(historyAt<0||historyAt>=steps.length-1){toast('没有可以重做的了');return;}
    historyAt+=1;applyState(steps[historyAt]);
  }
  // 删除后提示条上的「撤销」：删完没再改过就撤销这一步；之后又改过就提示用 Ctrl+Z
  function undoTo(mark){
    commit();
    if(steps[historyAt]?.n!==mark){toast('删除之后画布又改过了，请按 Ctrl+Z 一步步撤销');return;}
    undo();
  }
  function applyState(state){
    const keep=[...picked],panel=panelId;
    highWater=Math.max(highWater,board.nodeId);
    batching+=1;
    try{
      loadBoard({...state,seq:Math.max(seq,state.seq),nodes:state.nodes.map(n=>{
        if(n.kind!=='gen')return n;
        const latest=nodes.get(n.id)||genCache.get(n.id);          // 节点还在（或之前在过）：用它最新的图和生成状态
        return latest?{...n,...structuredClone(Object.fromEntries(GEN_FIELDS.map(k=>[k,latest[k]])))}:n;
      })});
      board.nodeId=Math.max(board.nodeId,highWater);              // 节点号不重复用，免得和撤销掉的节点对上
      renderGroups();background();
      setPicked(keep.filter(id=>nodes.has(id)));
      if(panel&&nodes.has(panel))openPanel(panel);else closePanel();
    }finally{batching-=1;}
    steps[historyAt]={...capture(),n:steps[historyAt].n};
    undoButtons();persist(0);schedulePoll();
  }
  async function flush(){
    if(!saveTimer)return;
    clearTimeout(saveTimer);saveTimer=null;
    await api(boardUrl(),snapshot(),'PUT').catch(error=>toast('画布没保存上：'+error.message));
  }
  async function flushPrefs(){
    if(!prefsTimer)return;
    clearTimeout(prefsTimer);prefsTimer=null;
    await api('/api/canvas/prefs',{gen:prefs.gen,presets:prefs.presets}).catch(()=>{});
  }
  // 在浏览器里直接关标签页时的补救：页面没了也能发出去（内容太大发不出就算了，桌面窗口走 window-chrome.js 的关窗口流程）
  window.addEventListener('pagehide',()=>{
    if(!saveTimer||!pid)return;
    clearTimeout(saveTimer);saveTimer=null;
    try{fetch(boardUrl(),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(snapshot()),keepalive:true}).catch(()=>{});}catch{}
  });
  function savePrefs(change){
    Object.assign(prefs,change);
    clearTimeout(prefsTimer);
    prefsTimer=setTimeout(()=>{prefsTimer=null;api('/api/canvas/prefs',{gen:prefs.gen,presets:prefs.presets}).catch(()=>{});},400);
  }

  // ── 节点卡片 ──
  const fileUrl=file=>`/api/canvas/projects/${encodeURIComponent(pid)}/files/${encodeURIComponent(file)}`;
  // 画布里的小图都用缩略图（服务端按需生成并缓存）：节点 1280、其余 256；看大图、复制、生图用原图
  const thumbUrl=(file,w=1280)=>`${fileUrl(file)}?w=${w}`;
  const element=id=>host.querySelector(`#node-${CSS.escape(String(id))}`);
  const current=node=>node?.results?.[node.current??0];
  let layoutRaf=0;
  const dirtyLayoutIds=new Set();
  function scheduleLayout(id){
    dirtyLayoutIds.add(String(id));
    if(layoutRaf)return;
    layoutRaf=requestAnimationFrame(()=>{
      layoutRaf=0;
      if(!board)return;
      const ids=[...dirtyLayoutIds];
      dirtyLayoutIds.clear();
      let panelDirty=false;
      for(const nid of ids){
        if(!element(nid))continue;
        board.updateConnectionNodes('node-'+nid);
        if(nid===panelId)panelDirty=true;
      }
      const all=boxes(),byId=new Map(all.map(b=>[b.id,b]));
      renderGroups(all,byId);
      syncActiveFlows();
      placeSelection();
      if(panelDirty){placePanel();revealPanel();}
    });
  }
  function render(id){
    const node=nodes.get(String(id)),el=element(id);
    if(!node||!el)return;
    const image=current(node),running=node.run?.state==='running',failed=node.run?.state==='failed';
    el.classList.toggle('cv-empty',!image);
    el.classList.toggle('cv-running',running);
    el.classList.toggle('picked',picked.has(String(id)));
    const started=node.run?.started||Date.now()/1000,expect=expectSeconds(node);
    const status=failed?'<span class="cv-status failed">失败</span>':'';
    const media=image?`<img src="${thumbUrl(image.file)}" alt="${escape(node.name)}" draggable="false" decoding="async">`:`<div class="cv-placeholder">${ICON.image}</div>`;
    const tools=image?`<div class="cv-tools cv-nodrag">
        ${node.kind==='gen'?`<button type="button" data-act="regen" title="用原来的提示词和设置再生一张，替换这张图" ${running?'disabled':''}>${ICON.regen}<span>重新生成</span></button>`:''}
        <button type="button" data-act="next" title="以这张为参考继续生成">${ICON.next}<span>继续生成</span></button>
        <button type="button" data-act="view" aria-label="看大图" title="看大图（双击图片）">${ICON.expand}</button>
        <button type="button" data-act="copy" aria-label="复制图片" title="复制图片，可以直接粘贴到微信、PS 等">${ICON.copy}</button>
        <button type="button" data-act="reveal" aria-label="在文件夹中显示" title="在文件夹中显示">${ICON.folder}</button>
        <button type="button" data-act="delete" aria-label="删除" title="删除（Delete）">${ICON.trash}</button></div>`
      :`<div class="cv-tools cv-nodrag"><button type="button" data-act="delete" aria-label="删除" title="删除（Delete）">${ICON.trash}</button></div>`;
    const progress=running?`${FLOWLIGHT}
      <div class="cv-progress" data-started="${started}" data-expect="${expect}">生图中 进度 <b>@@PCT@@</b></div>`:'';
    // 失败：节点上直接写原因、给「重试」，不用再点开面板找；有图的节点（重新生成失败）只在底部压一条，原图还看得见
    const reason=failed?String(node.run.error||'生成失败').replace(/\s+/g,' ').trim():'';
    const failure=failed?`<div class="cv-failed${image?' on-image':''}" title="${escape(reason)}">
        <b>${image?'重新生成失败':'生成失败'}</b><span>${escape(reason.length>140?reason.slice(0,140)+'…':reason)}</span>
        <div class="cv-nodrag"><button type="button" data-act="retry">重试</button>${image?'<button type="button" data-act="dismiss">忽略</button>':''}</div></div>`:'';
    const html=`<div class="cv-label">${ICON.image}<span>${escape(node.name)}</span>${status}</div>
      ${tools}<div class="cv-media" style="height:${mediaHeight(node)}px">${media}${progress}${failure}</div>`;
    const content=el.querySelector('.drawflow_content_node');
    if(content.dataset.sig===html)return;               // 内容没变：不重画，扫光动画和进度不会跳回去
    content.dataset.sig=html;
    content.innerHTML=html.replace('@@PCT@@',progressAt(Date.now()/1000-started,expect)+'%');
    // 多个节点同时渲染时共用同一个 rAF 重算连线与分组，消除逐节点重复量尺寸的 O(N²) 抖动
    scheduleLayout(id);
  }
  function renderAll(){for(const id of nodes.keys())render(id);}
  function expectSeconds(node){return Number(readDurations()[channelOf(node)?.id||''])||45;}
  // 一个生图节点的参考图（发给接口的顺序 = 面板上的编号）：先是连进来的分组里的图（按位置从上到下、从左到右），再是单独连的
  function refItems(id){
    id=String(id);const out=[],seen=new Set();
    const pos=m=>({y:data()[m]?.pos_y??0,x:data()[m]?.pos_x??0});
    for(const group of groups){
      if(!(group.links||[]).includes(id))continue;
      const skip=new Set(group.skip?.[id]||[]);
      const members=group.nodes.filter(m=>m!==id&&nodes.has(m)&&!skip.has(m)).sort((a,b)=>pos(a).y-pos(b).y||pos(a).x-pos(b).x);
      for(const m of members){
        const image=current(nodes.get(m));
        if(image&&!seen.has(m)){seen.add(m);out.push({id:m,name:nodes.get(m).name,image,via:group.id});}
      }
    }
    for(const edge of edges()){
      if(edge.to!==id||seen.has(edge.from))continue;
      const image=current(nodes.get(edge.from));
      if(image){seen.add(edge.from);out.push({id:edge.from,name:nodes.get(edge.from).name,image,via:null});}
    }
    return out;
  }
  function renderTargets(fromId){for(const edge of edges())if(edge.from===String(fromId))render(edge.to);}
  function boxes(){
    return [...nodes.keys()].map(id=>({id,x:data()[id]?.pos_x??0,y:data()[id]?.pos_y??0,w:NODE_WIDTH,h:element(id)?.offsetHeight||mediaHeight(nodes.get(id))}));
  }

  // ── 新建与排布（对齐格栅点） ──
  function create(kind,x,y,extra={}){
    seq+=1;
    const node={kind,name:nodeName(seq),results:[],current:0,gen:{prompt:'',...prefs.gen,channel:settings?.active||''},run:null,...extra};
    const spot=snapPoint({x,y});
    const id=board.addNode('image',kind==='gen'?1:0,1,spot.x,spot.y,CLASS[kind],{},'');
    node.id=String(id);nodes.set(node.id,node);
    render(id);
    return node.id;
  }
  function worldAt(clientX,clientY){
    const rect=host.getBoundingClientRect();
    return toWorld({x:board.canvas_x,y:board.canvas_y,zoom:board.zoom},{x:rect.left,y:rect.top},clientX,clientY);
  }
  function viewportCenter(){const rect=host.getBoundingClientRect();return worldAt(rect.left+rect.width/2,rect.top+rect.height/2);}
  function newGenAt(x,y,from){
    const id=batch(()=>{
      const id=create('gen',x,y);
      if(from)board.addConnection(from,id,'output_1','input_1');
      return id;
    });
    setPicked([id]);requestAnimationFrame(()=>$('cv-prompt').focus());save();
    return id;
  }
  function continueFrom(id){
    const spot=besideOf(boxes().find(b=>b.id===String(id)),boxes());
    return newGenAt(spot.x,spot.y,id);
  }
  function addImages(files,point){
    const center=point||viewportCenter();
    const spots=rowLayout({x:center.x-NODE_WIDTH/2-(files.length-1)*(NODE_WIDTH+GRID*2)/2,y:center.y-120},files.length);
    const ids=files.map((file,i)=>create('upload',spots[i].x,spots[i].y,{results:[file],current:0}));
    if(ids.length>1)setPicked(ids);
    save();
    return ids;
  }
  // 一个节点出了好几张：第一张放进这个节点（它还空着的话），其余每张一个节点，在下面同一列纵向排开，连线和设置跟原节点一样
  function placeResults(node,files){
    node.results=[...(node.results||[]),files[0]];node.current=node.results.length-1;render(node.id);
    stackResults(node,files.slice(1));
  }
  // 老版本一次出好几张：除了第一张，其余每张一个节点，在下面同一列纵向排开，连线和设置跟原节点一样
  function stackResults(node,rest){
    if(!rest.length)return;
    const source=boxes().find(b=>b.id===node.id);
    const spots=stackBelow(source,rest.map(f=>mediaHeight({results:[f]})),boxes());
    const sources=edges().filter(e=>e.to===node.id).map(e=>e.from);
    const group=groups.find(g=>g.nodes.includes(node.id));
    const linkedFrom=groups.filter(g=>(g.links||[]).includes(node.id));
    rest.forEach((file,i)=>{
      const id=create(node.kind,spots[i].x,spots[i].y,{results:[file],current:0,gen:{...node.gen},run:{state:'done'}});
      for(const from of sources)board.addConnection(from,id,'output_1','input_1');
      for(const g of linkedFrom)g.links.push(id);
      if(group)group.nodes.push(id);
    });
  }

  // ── 选择（单选出面板，多选出「打组」） ──
  function setPicked(ids,group=null,lines=[]){
    pickedLines=new Set(lines);markLines();
    picked=new Set(ids.map(String).filter(id=>nodes.has(id)));
    pickedGroup=group||(picked.size>1?selectionGroup()?.id:null)||null;      // 框选正好选中一整组，也算选中这个组
    for(const el of host.querySelectorAll('.drawflow-node'))el.classList.toggle('picked',picked.has(el.id.slice(5)));
    host.querySelectorAll('.cv-group').forEach(el=>el.classList.toggle('picked',el.dataset.group===pickedGroup));
    board?.node_selected?.classList.remove('selected');
    if(board)board.node_selected=null;
    if(picked.size===1){
      const [id]=picked;
      if(board){board.node_selected=element(id);board.node_selected?.classList.add('selected');}
      openPanel(id);
    }else closePanel();
    placeSelection();
  }
  // 选中的正好是一个组的全部成员：按钮变成「解组」
  const selectionGroup=()=>groups.find(g=>g.nodes.length===picked.size&&g.nodes.every(id=>picked.has(id)))||null;
  function placeSelection(metrics=null){
    const box=$('cv-selbox'),bar=$('cv-selbar');
    if(picked.size<2||!board){box.hidden=true;bar.hidden=true;return;}
    const group=selectionGroup();
    const w=metrics?.wrapRect||wrap.getBoundingClientRect();
    const s=metrics?.hostRect||host.getBoundingClientRect();
    let left,top;
    if(metrics?.boxById){
      const z=board.zoom;
      const sx=v=>s.left+(metrics.hostClientLeft??host.clientLeft)+board.canvas_x+v*z;
      const sy=v=>s.top+(metrics.hostClientTop??host.clientTop)+board.canvas_y+v*z;
      if(group){
        const gf=groupFrame(group.nodes.map(id=>metrics.boxById.get(id)).filter(Boolean));
        left=sx(gf.x);top=sy(gf.y);box.hidden=true;
      }else{
        const pickedBoxes=[...picked].map(id=>metrics.boxById.get(id)).filter(Boolean);
        if(!pickedBoxes.length){box.hidden=true;bar.hidden=true;return;}
        const pad=10;
        left=Math.min(...pickedBoxes.map(b=>sx(b.x)))-pad;
        top=Math.min(...pickedBoxes.map(b=>sy(b.y-28)))-pad;
        const right=Math.max(...pickedBoxes.map(b=>sx(b.x+b.w)))+pad;
        const bottom=Math.max(...pickedBoxes.map(b=>sy(b.y+b.h)))+pad;
        Object.assign(box.style,{left:left-w.left+'px',top:top-w.top+'px',width:right-left+'px',height:bottom-top+'px'});
        box.hidden=false;
      }
    }else{
      const frame=group&&board.precanvas.querySelector(`.cv-group[data-group="${CSS.escape(group.id)}"]`);
      if(frame){
        // 选中的正好是一个组：组框本身就高亮了，不再画虚线外框，按钮放在组框上面
        const r=frame.getBoundingClientRect();left=r.left;top=r.top;box.hidden=true;
      }else{
        // 外框把卡片上方的名字也包进去
        const rects=[...picked].flatMap(id=>{const el=element(id);return el?[el,el.querySelector('.cv-label')].filter(Boolean).map(e=>e.getBoundingClientRect()):[];});
        if(!rects.length){box.hidden=true;bar.hidden=true;return;}
        const pad=10;
        left=Math.min(...rects.map(r=>r.left))-pad;top=Math.min(...rects.map(r=>r.top))-pad;
        const right=Math.max(...rects.map(r=>r.right))+pad,bottom=Math.max(...rects.map(r=>r.bottom))+pad;
        Object.assign(box.style,{left:left-w.left+'px',top:top-w.top+'px',width:right-left+'px',height:bottom-top+'px'});
        box.hidden=false;
      }
    }
    const groupKey=group?'ungroup':'group';
    if($('cv-group').dataset.mode!==groupKey){
      $('cv-group').dataset.mode=groupKey;
      $('cv-group').innerHTML=`${group?ICON.ungroup:ICON.group}<span>${group?'解组':'打组'}</span><kbd>Ctrl G</kbd>`;
      $('cv-group').title=group?'解散这个组':'把选中的图片打成一组';
    }
    bar.hidden=false;
    const barH=metrics?.barHeight||bar.offsetHeight;
    bar.style.left=Math.max(s.left-w.left+8,left-w.left)+'px';
    bar.style.top=Math.max(s.top-w.top+8,top-w.top-barH-8)+'px';
  }
  $('cv-group').onclick=()=>toggleGroup();
  function toggleGroup(){
    if(picked.size<2)return;
    const existing=selectionGroup();
    if(existing){
      groups=groups.filter(g=>g!==existing);
      for(const to of existing.links||[])for(const from of existing.nodes)
        if(nodes.has(from)&&nodes.has(to)&&!edges().some(e=>e.from===from&&e.to===to))board.addConnection(from,to,'output_1','input_1');
      renderGroups();setPicked([...picked]);
      toast(existing.links?.length?`已解组「${existing.name}」，组里的图改成单独连着`:`已解组「${existing.name}」`);
    }else{
      const ids=[...picked];
      groups=groups.map(g=>({...g,nodes:g.nodes.filter(id=>!picked.has(id))})).filter(g=>g.nodes.length>1);   // 一个节点只属于一个组
      const number=Math.max(0,...groups.map(g=>Number(String(g.name).match(/\d+/)?.[0])||0))+1;
      const group={id:'g'+Date.now().toString(36),name:`分组 ${number}`,nodes:ids};
      groups.push(group);renderGroups();setPicked(ids,group.id);
    }
    save();
  }

  // ── 分组框（在画布里，跟着缩放；拖外框整组移动，双击组名改名） ──
  function renderGroups(preBoxes=null,preMap=null){
    if(!board)return;
    const keep=new Set(),all=preBoxes||boxes(),byId=preMap||new Map(all.map(b=>[b.id,b]));
    for(const group of groups){
      const members=group.nodes.filter(id=>nodes.has(id));
      if(members.length<2)continue;
      keep.add(group.id);
      let el=board.precanvas.querySelector(`.cv-group[data-group="${CSS.escape(group.id)}"]`);
      if(!el){
        el=document.createElement('div');el.className='cv-group';el.dataset.group=group.id;
        el.innerHTML=`<span class="cv-group-label">${ICON.group}<span></span></span>
          <span class="cv-group-port" title="拉线到生图节点：这组图都当参考图（拉到空白处新建一个）"></span>`;
        board.precanvas.prepend(el);                    // 放在最底层，不挡节点和连线
      }
      const frame=groupFrame(members.map(id=>byId.get(id)).filter(Boolean));
      Object.assign(el.style,{left:frame.x+'px',top:frame.y+'px',width:frame.w+'px',height:frame.h+'px'});
      const titleEl=el.querySelector('.cv-group-label>span');
      if(titleEl&&titleEl.textContent!==group.name)titleEl.textContent=group.name;
      el.classList.toggle('picked',pickedGroup===group.id);
    }
    board.precanvas.querySelectorAll('.cv-group').forEach(el=>{if(!keep.has(el.dataset.group))el.remove();});
    groups=groups.filter(g=>keep.has(g.id));
    renderGroupLinks(all,byId);
  }

  // ── 分组连线：从组框右边的「+」到生图节点左边的「+」，一条线代表整组图都是参考图 ──
  function linkLayer(){
    let svg=board.precanvas.querySelector('svg.cv-glinks');
    if(!svg){
      svg=document.createElementNS(SVG_NS,'svg');svg.setAttribute('class','cv-glinks');
      const frames=board.precanvas.querySelectorAll('.cv-group');
      if(frames.length)frames[frames.length-1].after(svg);else board.precanvas.prepend(svg);   // 在组框上面、节点下面
    }
    return svg;
  }
  const GROUP_PORT=34;      // 组的「+」圆心离组框右边多远（CSS：right:-47px、宽 26）
  const linkStart=(group,byId=null)=>{
    const memberBoxes=byId
      ?group.nodes.map(id=>byId.get(id)).filter(Boolean)
      :boxes().filter(b=>group.nodes.includes(b.id));
    const frame=groupFrame(memberBoxes);
    return {x:frame.x+frame.w+GROUP_PORT,y:frame.y+frame.h/2};     // 组框右边正中「+」的圆心（和 CSS 对应）
  };
  function renderGroupLinks(preBoxes=null,preMap=null){
    if(!board)return;
    const all=preBoxes||boxes(),byId=preMap||new Map(all.map(b=>[b.id,b])),paths=[];
    const isRunning=id=>nodes.get(String(id))?.run?.state==='running';
    for(const group of groups){
      group.links=(group.links||[]).filter(id=>nodes.get(id)?.kind==='gen'&&!group.nodes.includes(id));
      if(!group.links.length)continue;
      const start=linkStart(group,byId);
      const groupRunning=group.nodes.some(isRunning);
      for(const to of group.links){
        const box=byId.get(to);if(!box)continue;
        const d=curvePath(start.x,start.y,box.x-23,box.y+box.h/2);         // 节点左边「+」的圆心
        const active=pickedLines.has(`g:${group.id}>${to}`);
        const flowing=groupRunning||isRunning(to);
        paths.push(`<path class="cv-ghit" data-group="${group.id}" data-to="${to}" d="${d}"/><path class="cv-glink${active?' selected':''}" data-group="${group.id}" data-to="${to}" d="${d}"/><path class="cv-flow${flowing?' is-flowing':''}" data-group="${group.id}" data-to="${to}" d="${d}"/>`);
      }
    }
    const svg=board.precanvas.querySelector('svg.cv-glinks');
    if(paths.length||svg)linkLayer().innerHTML=paths.join('');
  }
  function addLink(group,id){
    group.links=[...new Set([...(group.links||[]),String(id)])];
    renderGroupLinks();
    if(panelId===String(id))renderPanel();
    save();
  }
  function removeLink({group:gid,to}){
    const group=groups.find(g=>g.id===gid);if(!group)return;
    group.links=(group.links||[]).filter(id=>id!==to);
    if(group.skip)delete group.skip[to];
    pickedLines.delete(`g:${gid}>${to}`);renderGroupLinks();
    if(panelId===to)renderPanel();
    save();toast(`已断开「${group.name}」的连线`);
  }
  // 面板里去掉分组连进来的某一张；一张都不剩就断开这条分组连线
  function skipMember(gid,to,member){
    const group=groups.find(g=>g.id===gid);if(!group)return;
    group.skip={...(group.skip||{}),[to]:[...new Set([...(group.skip?.[to]||[]),member])]};
    if(!refItems(to).some(r=>r.via===gid))return removeLink({group:gid,to});
    renderGroupLinks();if(panelId===to)renderPanel();save();
  }
  function unskipMember(gid,to,member){
    const group=groups.find(g=>g.id===gid);if(!group?.skip?.[to])return;
    group.skip[to]=group.skip[to].filter(id=>id!==member);
    if(panelId===to)renderPanel();save();
  }
  // ── 连线的选中：点一下选中（Shift 加选），框选时碰到的线也选中；按 Delete 删除 ──
  const lineKey=path=>{
    if(path.classList.contains('cv-glink'))return `g:${path.dataset.group}>${path.dataset.to}`;
    const cls=path.parentElement?.getAttribute('class')||'';
    const from=cls.match(/node_out_node-(\S+)/)?.[1],to=cls.match(/node_in_node-(\S+)/)?.[1];
    return from&&to?`e:${from}>${to}`:null;
  };
  const linePaths=()=>[...host.querySelectorAll('svg.connection .main-path,.cv-glink:not(.temp)')].filter(path=>lineKey(path));
  function markLines(){
    if(!board)return;
    const live=new Set();
    for(const path of linePaths()){const key=lineKey(path);live.add(key);path.classList.toggle('selected',pickedLines.has(key));}
    for(const key of [...pickedLines])if(!live.has(key))pickedLines.delete(key);       // 线已经没了
  }
  // 框选：线上每隔几个像素取一个点，有一个点落在框里就算选中
  function linesInRect(r){
    const found=[];
    for(const path of linePaths()){
      const matrix=path.getScreenCTM();if(!matrix)continue;
      const length=path.getTotalLength(),step=Math.max(2,6/(board.zoom||1));
      for(let at=0;at<=length;at+=step){
        const q=path.getPointAtLength(at),p=new DOMPoint(q.x,q.y).matrixTransform(matrix);
        if(p.x>=r.left&&p.x<=r.right&&p.y>=r.top&&p.y<=r.bottom){found.push(lineKey(path));break;}
      }
    }
    return found;
  }
  function pickLine(key,additive){
    if(additive){
      const lines=new Set(pickedLines);lines.has(key)?lines.delete(key):lines.add(key);
      setPicked([...picked],null,[...lines]);
    }else setPicked([],null,[key]);
  }
  // 从组框的「+」拉线：松手在生图节点上就连上；松手在空白处就在那里新建一个生图节点再连上
  function beginLinkDrag(group,event){
    const temp=document.createElementNS(SVG_NS,'path');temp.setAttribute('class','cv-glink temp');linkLayer().append(temp);
    const start=linkStart(group),hostRect=host.getBoundingClientRect();host.classList.add('cv-linking');
    let linkRaf=0,lastEvent=event;
    const drawTemp=()=>{
      linkRaf=0;
      const p=toWorld({x:board.canvas_x,y:board.canvas_y,zoom:board.zoom},{x:hostRect.left,y:hostRect.top},lastEvent.clientX,lastEvent.clientY);
      temp.setAttribute('d',curvePath(start.x,start.y,p.x,p.y));
    };
    const onMove=e=>{lastEvent=e;if(!linkRaf)linkRaf=requestAnimationFrame(drawTemp);};
    const onUp=e=>{
      window.removeEventListener('mousemove',onMove);
      if(linkRaf){cancelAnimationFrame(linkRaf);linkRaf=0;}
      temp.remove();host.classList.remove('cv-linking');
      const under=document.elementFromPoint(e.clientX,e.clientY);
      const target=under?.closest?.('#canvas-stage .drawflow-node');
      if(target){
        const id=target.id.slice(5),node=nodes.get(id);
        if(!node)return;
        if(node.kind!=='gen')return toast('只能连到生图节点（上传的图片不能生图）');
        if(group.nodes.includes(id))return toast('不能连到这个组里的节点');
        setPicked([id]);addLink(group,id);toast(`「${group.name}」的图都成了「${node.name}」的参考图`);
        return;
      }
      if(isEmpty(under)){const p=worldAt(e.clientX,e.clientY);const id=newGenAt(p.x,p.y-90);addLink(group,id);}
    };
    drawTemp();
    window.addEventListener('mousemove',onMove);
    window.addEventListener('mouseup',onUp,{once:true});
  }
  // 这个监听比 Drawflow 的先注册，按在分组框上时拦下来，不让 Drawflow 处理
  host.addEventListener('mousedown',event=>{
    if(event.button!==0)return;
    const link=event.target.closest?.('.cv-ghit,.cv-glink');
    if(link&&!link.classList.contains('temp')){
      event.stopImmediatePropagation();event.preventDefault();host.focus();
      pickLine(`g:${link.dataset.group}>${link.dataset.to}`,event.shiftKey||event.ctrlKey||event.metaKey);
      return;
    }
    const line=event.target.closest?.('svg.connection .cv-hit,svg.connection .main-path');
    const key=line&&lineKey(line.parentElement.querySelector('.main-path'));
    if(key){
      event.stopImmediatePropagation();event.preventDefault();host.focus();
      board.connection_selected=null;
      pickLine(key,event.shiftKey||event.ctrlKey||event.metaKey);
      return;
    }
    const port=event.target.closest?.('.cv-group-port');
    if(port){
      const group=groups.find(g=>g.id===port.parentElement.dataset.group);if(!group)return;
      event.stopImmediatePropagation();event.preventDefault();host.focus();
      beginLinkDrag(group,event);return;
    }
    const frame=event.target.closest?.('.cv-group');
    if(!frame)return;
    const group=groups.find(g=>g.id===frame.dataset.group);if(!group)return;
    event.stopImmediatePropagation();event.preventDefault();host.focus();
    setPicked(group.nodes,group.id);
    beginMove(group.nodes,event);
  });
  host.addEventListener('dblclick',event=>{
    const label=event.target.closest?.('.cv-group-label');if(!label)return;
    const group=groups.find(g=>g.id===label.parentElement.dataset.group);if(!group)return;
    event.stopImmediatePropagation();
    const name=label.querySelector('span'),input=document.createElement('input');
    input.className='cv-rename';input.value=group.name;input.maxLength=30;
    name.replaceWith(input);input.focus();input.select();
    let done=false;
    const finish=commit=>{
      if(done)return;done=true;
      const value=input.value.trim();
      if(commit&&value)group.name=value;
      input.replaceWith(name);renderGroups();save();
    };
    input.addEventListener('mousedown',e=>e.stopImmediatePropagation());
    input.addEventListener('keydown',e=>{e.stopPropagation();if(e.key==='Enter')finish(true);if(e.key==='Escape')finish(false);});
    input.addEventListener('blur',()=>finish(true));
  });

  // ── 拖动：对齐别的节点（参考线）+ 对齐格栅 ──
  // 拖动开始时一次性缓存全部节点尺寸与容器几何，mousemove 仅做纯数学计算并通过单个 rAF 合并位置与连线重绘。
  function beginMove(ids,event,onClick){
    ids=ids.filter(id=>nodes.has(id));
    if(!ids.length)return;
    const start=new Map(ids.map(id=>[id,{x:data()[id].pos_x,y:data()[id].pos_y}]));
    const allBoxes=boxes().map(b=>({...b}));
    const boxById=new Map(allBoxes.map(b=>[b.id,b]));
    const sizes=new Map(ids.map(id=>[id,{...boxById.get(id)}]).filter(([,b])=>b));
    const others=allBoxes.filter(b=>!start.has(b.id));
    const dragMetrics={
      hostRect:host.getBoundingClientRect(),
      wrapRect:wrap.getBoundingClientRect(),
      hostClientLeft:host.clientLeft,
      hostClientTop:host.clientTop,
      panelWidth:panel.hidden?0:panel.offsetWidth,
      barHeight:$('cv-selbar').hidden?38:($('cv-selbar').offsetHeight||38),
      allBoxes,
      boxById,
    };
    const [first]=start.values();
    let moved=false,moveRaf=0,pendingMove=null;
    const flushMove=()=>{
      moveRaf=0;
      if(!pendingMove)return;
      const {dx,dy,lines}=pendingMove;
      pendingMove=null;
      applyMove(start,dx,dy,dragMetrics);
      drawGuides(lines,dragMetrics);
    };
    const onMove=e=>{
      if(!moved&&Math.hypot(e.clientX-event.clientX,e.clientY-event.clientY)<3)return;
      if(!moved){moved=true;host.classList.add('cv-dragging');}
      let dx=(e.clientX-event.clientX)/board.zoom,dy=(e.clientY-event.clientY)/board.zoom;
      let lines=[];
      if(!e.altKey){
        const bbox=boundsOf(ids.map(id=>({...sizes.get(id),x:start.get(id).x+dx,y:start.get(id).y+dy})));
        const guide=alignGuides(bbox,others,ALIGN_THRESHOLD/board.zoom);
        // 对上别的节点的方向按参考线走；其余方向一格一格对齐格栅点（整组一起挪，相对位置不变）
        dx=guide.snappedX?dx+guide.dx:snap(first.x+dx)-first.x;
        dy=guide.snappedY?dy+guide.dy:snap(first.y+dy)-first.y;
        lines=guide.lines;
      }
      pendingMove={dx,dy,lines};
      if(!moveRaf)moveRaf=requestAnimationFrame(flushMove);
    };
    const onUp=()=>{
      window.removeEventListener('mousemove',onMove);
      if(moveRaf){cancelAnimationFrame(moveRaf);moveRaf=0;}
      if(pendingMove)flushMove();
      host.classList.remove('cv-dragging');drawGuides([],dragMetrics);
      if(moved){placePanel();placeSelection();save();}else onClick?.();
    };
    window.addEventListener('mousemove',onMove);
    window.addEventListener('mouseup',onUp,{once:true});
  }
  function applyMove(start,dx,dy,metrics=null){
    for(const [id,from] of start){
      const entry=data()[id],el=element(id);if(!entry||!el)continue;
      entry.pos_x=from.x+dx;entry.pos_y=from.y+dy;
      el.style.left=entry.pos_x+'px';el.style.top=entry.pos_y+'px';
      const cached=metrics?.boxById?.get(id);
      if(cached){cached.x=entry.pos_x;cached.y=entry.pos_y;}
      board.updateConnectionNodes('node-'+id);
    }
    renderGroups(metrics?.allBoxes||null,metrics?.boxById||null);
    placePanel(metrics);
    placeSelection(metrics);
  }
  function drawGuides(lines,metrics=null){
    const layer=$('cv-guides');
    if(!lines.length){if(layer.innerHTML)layer.innerHTML='';return;}
    const s=metrics?.hostRect||host.getBoundingClientRect();
    const w=metrics?.wrapRect||wrap.getBoundingClientRect();
    const cl=metrics?.hostClientLeft??host.clientLeft,ct=metrics?.hostClientTop??host.clientTop;
    const z=board.zoom;
    const sx=v=>s.left-w.left+cl+board.canvas_x+v*z,sy=v=>s.top-w.top+ct+board.canvas_y+v*z;
    layer.innerHTML=lines.map(l=>l.axis==='x'
      ?`<i class="v" style="left:${sx(l.at)}px;top:${sy(l.from)-16}px;height:${(l.to-l.from)*z+32}px"></i>`
      :`<i class="h" style="top:${sy(l.at)}px;left:${sx(l.from)-16}px;width:${(l.to-l.from)*z+32}px"></i>`).join('');
  }

  // ── 框选 ──
  function beginMarquee(event){
    const box=$('cv-marquee'),w=wrap.getBoundingClientRect(),additive=event.shiftKey||event.ctrlKey||event.metaKey;
    const base=additive?new Set(picked):new Set(),baseLines=additive?new Set(pickedLines):new Set();
    let moved=false;
    const rectOf=e=>({left:Math.min(event.clientX,e.clientX),top:Math.min(event.clientY,e.clientY),
      right:Math.max(event.clientX,e.clientX),bottom:Math.max(event.clientY,e.clientY)});
    const hits=r=>[...host.querySelectorAll('.drawflow-node')].filter(el=>{
      const b=el.getBoundingClientRect();return b.left<r.right&&r.left<b.right&&b.top<r.bottom&&r.top<b.bottom;}).map(el=>el.id.slice(5));
    const onMove=e=>{
      if(!moved&&Math.hypot(e.clientX-event.clientX,e.clientY-event.clientY)<4)return;
      if(!moved){moved=true;closePanel();}
      const r=rectOf(e);
      Object.assign(box.style,{left:r.left-w.left+'px',top:r.top-w.top+'px',width:r.right-r.left+'px',height:r.bottom-r.top+'px'});
      box.hidden=false;
      const live=new Set([...base,...hits(r)]),lines=new Set([...baseLines,...linesInRect(r)]);
      for(const el of host.querySelectorAll('.drawflow-node'))el.classList.toggle('picked',live.has(el.id.slice(5)));
      for(const path of linePaths())path.classList.toggle('selected',lines.has(lineKey(path)));
    };
    const onUp=e=>{
      window.removeEventListener('mousemove',onMove);box.hidden=true;
      if(!moved){if(!additive)setPicked([]);return;}
      const r=rectOf(e);
      setPicked([...new Set([...base,...hits(r)])],null,[...new Set([...baseLines,...linesInRect(r)])]);
    };
    window.addEventListener('mousemove',onMove);
    window.addEventListener('mouseup',onUp,{once:true});
  }

  // ── 生成 ──
  async function run(id,{single=false}={}){
    const node=nodes.get(String(id));
    if(!node||node.run?.state==='running')return;
    if(!node.gen.prompt.trim()){setPicked([node.id]);toast('先写提示词');$('cv-prompt').focus();return;}
    const channel=channelOf(node);
    if(!channel?.model){channelsUI.open(channel?.id).catch(error=>toast(error.message));toast('先在生图渠道里配好模型');return;}
    const items=refItems(node.id);
    const job={prompt:resolveMentions(node.gen.prompt,items).text,refs:items.map(r=>r.image.file)};
    const count=Math.max(1,Number(node.gen.count)||1);
    if(!single&&!current(node)&&count>1)return branch(node,channel,job,count);   // 重试只重做这一个节点，不再分出新节点
    await startRun(node,channel,job);                 // 空节点出一张，或者有图的节点重新生成（出一张替换）
  }
  async function startRun(node,channel,{prompt,refs},{deferSchedule=false}={}){
    const {ratio,quality,resolution}=node.gen;
    try{
      const record=await api('/api/canvas/generate',{project:pid,node:node.id,prompt,ratio,quality,resolution,count:1,refs,channel:channel.id});
      runPollFailures=0;
      node.run={id:record.id,state:'running',started:record.started,error:''};
      render(node.id);if(panelId===node.id)renderPanel();
      if(!deferSchedule){save(0);schedulePoll();}
    }catch(error){
      node.run={state:'failed',error:error.message};render(node.id);if(panelId===node.id)renderPanel();toast(error.message);
    }
  }
  // 脑图：空节点当提示词节点，右边立刻分出 count 个连着它的节点（马上显示流光和进度），各自单独生成、谁先好谁先出图
  function branch(node,channel,job,count){
    const source=boxes().find(b=>b.id===node.id);
    const height=mediaHeight({kind:'gen',gen:node.gen,results:[]});
    const spots=branchLayout(source,Array(count).fill(height),boxes());
    const children=batch(()=>{
      const list=spots.map(spot=>create('gen',spot.x,spot.y,{gen:{...node.gen}}));
      for(const child of list)board.addConnection(node.id,child,'output_1','input_1');
      return list;
    });
    save(0);
    requestAnimationFrame(()=>showAll([node.id,...children]));
    Promise.allSettled(children.map(child=>startRun(nodes.get(child),channel,job,{deferSchedule:true}))).then(()=>{
      save(0);schedulePoll();
    });
  }
  // 让这些节点都在画面里：已经都看得见就不动；看不全就平移（必要时缩小）到正好装下
  function showAll(ids){
    const list=boxes().filter(b=>ids.includes(b.id));if(!list.length)return;
    const s=host.getBoundingClientRect(),view={x:board.canvas_x,y:board.canvas_y,zoom:board.zoom};
    const visible=list.every(b=>{
      const left=view.x+b.x*view.zoom,top=view.y+(b.y-30)*view.zoom;
      return left>=0&&top>=0&&left+b.w*view.zoom<=s.width&&top+(b.h+30)*view.zoom<=s.height-90;
    });
    if(visible)return;
    const fit=fitView(list.map(b=>({...b,y:b.y-30,h:b.h+30})),s.width,s.height-90,60),zoom=Math.min(fit.zoom,board.zoom);  // 只缩小不放大
    const cx=(Math.min(...list.map(b=>b.x))+Math.max(...list.map(b=>b.x+b.w)))/2;
    const cy=(Math.min(...list.map(b=>b.y-30))+Math.max(...list.map(b=>b.y+b.h)))/2;
    animateView({zoom,x:s.width/2-cx*zoom,y:(s.height-90)/2-cy*zoom},360);
  }
  let runPollFailures=0,pollingRuns=false;
  const runningNodes=()=>[...nodes.values()].filter(n=>n.run?.state==='running');
  function stopPollTimers(){
    clearTimeout(pollTimer);pollTimer=null;
    clearInterval(tickTimer);tickTimer=null;
  }
  function updateProgressText(){
    const live=host.querySelectorAll('.cv-progress');
    if(!live.length){clearInterval(tickTimer);tickTimer=null;return;}
    for(const el of live){
      const text=progressAt(Date.now()/1000-Number(el.dataset.started),Number(el.dataset.expect))+'%',b=el.querySelector('b');
      if(b&&b.textContent!==text)b.textContent=text;
    }
  }
  function schedulePoll(delayOverride){
    clearTimeout(pollTimer);pollTimer=null;
    const count=runningNodes().length;
    syncActiveFlows();
    if(!count||!active()||document.hidden){
      clearInterval(tickTimer);tickTimer=null;
      return;
    }
    const delay=delayOverride??canvasPollDelay(count,runPollFailures);
    pollTimer=setTimeout(poll,delay);
    if(!tickTimer){
      updateProgressText();
      tickTimer=setInterval(()=>{
        if(!active()||document.hidden){clearInterval(tickTimer);tickTimer=null;return;}
        updateProgressText();
      },500);
    }
  }
  async function poll(){
    if(!active()||document.hidden||pollingRuns)return;
    const list=runningNodes();
    if(!list.length){stopPollTimers();syncActiveFlows();return;}
    pollingRuns=true;
    const at=cid;
    let changed=false,succeeded=0,transientError=null;
    const pollOne=async node=>{
      if(at!==cid||node.run?.state!=='running')return;
      try{
        const record=await api('/api/canvas/runs/'+node.run.id);
        succeeded+=1;
        if(at!==cid)return;
        if(record.state==='running')return;
        if(record.state==='done'){
          learnDuration(record.channel||channelOf(node)?.id,(record.finished||0)-(record.started||0));
          node.run={id:record.id,state:'done',error:''};
          placeResults(node,record.files);
          if(record.error)toast(`${node.name}：${record.error}`);
        }else{
          node.run={id:record.id,state:'failed',error:record.error||'生成失败'};
        }
        render(node.id);renderTargets(node.id);
        changed=true;
      }catch(error){
        if(at!==cid)return;
        if(error?.status===404){
          node.run={state:'failed',error:'找不到这次生成的记录，请重试。'};
          render(node.id);changed=true;
          return;
        }
        transientError=error;
      }
    };
    try{
      // 已经连续报错时先探路 1 个节点，避免离线时多节点同时打满报错；正常时按每批 3 个合并并发轮询。
      let idx=0;
      if(runPollFailures>0&&list.length){
        await pollOne(list[0]);
        idx=1;
      }
      while(!transientError&&idx<list.length&&at===cid){
        const chunk=list.slice(idx,idx+3);
        idx+=chunk.length;
        await Promise.all(chunk.map(pollOne));
      }
      if(at!==cid)return;
      if(transientError&&!succeeded){
        runPollFailures+=1;
        if(runPollFailures===2){
          toast('连接生图服务不稳定，正在自动重试…');
        }else if(runPollFailures>=MAX_CANVAS_POLL_FAILURES){
          const msg='无法连接本地服务，已停止等待生图结果；服务恢复后可点「重试」继续生成。';
          for(const stuck of runningNodes()){
            stuck.run={...stuck.run,state:'failed',error:msg};
            render(stuck.id);
            changed=true;
          }
          runPollFailures=0;
          toast('连续多次连不上后台服务，已停止等待生图结果，请检查服务后点节点上的「重试」。');
        }
      }else if(succeeded>0&&!transientError){
        runPollFailures=0;
      }
      if(changed){
        renderGroups();
        if(panelId)renderPanel();
        syncActiveFlows();
        if(!transientError?.offline)save(0);
      }
    }finally{
      pollingRuns=false;
      if(at===cid)schedulePoll();
    }
  }

  // ── 节点上的小工具条 ──
  host.addEventListener('click',guard(async event=>{
    const button=event.target.closest('[data-act]');if(!button)return;
    const id=button.closest('.drawflow-node')?.id.slice(5),node=nodes.get(id);
    if(!node)return;
    const act=button.dataset.act;
    if(act==='regen')run(id);
    else if(act==='retry')run(id,{single:true});
    else if(act==='dismiss'){node.run=null;render(id);if(panelId===id)renderPanel();save(0);}
    else if(act==='next')continueFrom(id);
    else if(act==='view')viewNode(node);
    else if(act==='copy'){await copyImage(fileUrl(current(node).file));toast('已复制图片，可以直接粘贴到微信、PS 等');}
    else if(act==='reveal')await api('/api/canvas/reveal',{project:pid,file:current(node).file});
    else if(act==='delete')remove([id]);
  }));
  function viewNode(node){
    const images=[...nodes.values()].filter(current);
    openLightbox(images.map(n=>({src:fileUrl(current(n).file),caption:n.name})),Math.max(0,images.indexOf(node)));
  }
  function remove(ids,lines=[]){
    ids=ids.map(String).filter(id=>nodes.has(id));
    const gone=new Set(ids);
    // 选中的连线里，不跟着节点一起没的才单独删
    const cutEdges=lines.filter(k=>k.startsWith('e:')).map(k=>{const [from,to]=k.slice(2).split('>');return {from,to};})
      .filter(e=>!gone.has(e.from)&&!gone.has(e.to)&&edges().some(x=>x.from===e.from&&x.to===e.to));
    const cutLinks=lines.filter(k=>k.startsWith('g:')).map(k=>{const [group,to]=k.slice(2).split('>');return {group,to};})
      .filter(l=>!gone.has(l.to)&&groups.some(g=>g.id===l.group&&(g.links||[]).includes(l.to)));
    if(!ids.length&&!cutEdges.length&&!cutLinks.length)return;
    commit();
    batching+=1;
    let names;
    try{names=removeNow(ids,gone,cutEdges,cutLinks);}finally{batching-=1;}
    save(0);
    const mark=steps[historyAt]?.n,count=cutEdges.length+cutLinks.length;
    if(!ids.length)return toastAction(`已删除 ${count} 条连线`,'撤销',async()=>undoTo(mark));
    const lineText=count?`、${count} 条连线`:'';
    toastAction(names.length>1||lineText?`已删除 ${names.length} 个节点${lineText}`:`已删除「${names[0]}」`,'撤销',async()=>undoTo(mark));
  }
  function removeNow(ids,gone,cutEdges,cutLinks){
    for(const e of cutEdges)board.removeSingleConnection(e.from,e.to,'output_1','input_1');
    for(const l of cutLinks){
      const group=groups.find(g=>g.id===l.group);
      group.links=group.links.filter(id=>id!==l.to);if(group.skip)delete group.skip[l.to];
    }
    if(!ids.length){setPicked([]);renderGroupLinks();if(panelId)renderPanel();return [];}
    const names=ids.map(id=>nodes.get(id).name),targets=edges().filter(e=>gone.has(e.from)&&!gone.has(e.to)).map(e=>e.to);
    for(const id of ids){nodes.delete(id);board.removeNodeId('node-'+id);}
    groups=groups.map(g=>({...g,nodes:g.nodes.filter(id=>!gone.has(id)),links:(g.links||[]).filter(id=>!gone.has(id)),
      skip:Object.fromEntries(Object.entries(g.skip||{}).filter(([to])=>!gone.has(to)))}));
    setPicked([]);for(const id of targets)render(id);renderGroups();
    return names;
  }

  // ── 输入面板（单选一个生图节点时，出现在它下面） ──
  function openPanel(id){
    const node=nodes.get(String(id));
    if(!node||node.kind!=='gen'){closePanel();return;}
    const fresh=panelId!==String(id);
    if(fresh)closePop();
    panelId=String(id);panel.hidden=false;
    renderPanel();placePanel();
    if(fresh)requestAnimationFrame(revealPanel);
  }
  function closePanel(){panelId=null;panel.hidden=true;closePop();closeMention();}
  function renderPanel(){
    const node=nodes.get(panelId);if(!node)return closePanel();
    const refs=refItems(node.id);
    $('cv-ref-row').innerHTML=refs.map((r,i)=>{
      const close=r.via?`data-skip="${escape(r.via)}" data-id="${escape(r.id)}"`:`data-unref="${escape(r.id)}"`;
      return `<span class="cv-ref"><img src="${thumbUrl(r.image.file,256)}" alt="${escape(r.name)}" title="图${i+1}：${escape(r.name)}（提示词里可以 @${escape(r.name)}）"><b>${i+1}</b>
        <button type="button" ${close} aria-label="不再参考${escape(r.name)}">${ICON.close}</button></span>`;
    }).join('');
    $('cv-ref-row').hidden=!refs.length;
    if(document.activeElement!==$('cv-prompt'))$('cv-prompt').value=node.gen.prompt||'';
    const channel=channelOf(node);
    $('cv-model').innerHTML=`${ICON.model}<span>${escape(channel?channelLabel(channel):'选择生图渠道')}</span>${ICON.down}`;
    $('cv-params').innerHTML=`${ratioIcon(node.gen.ratio)}<span>${escape(paramsText(node.gen))}</span>${ICON.down}`;
    const running=node.run?.state==='running';
    $('cv-go').disabled=running;
    $('cv-go').innerHTML=running?'<span>生成中…</span>':current(node)?`${ICON.regen}<span>重新生成</span>`:`${ICON.bolt}<span>生成</span>`;
    $('cv-go').title=current(node)?'用这里的提示词和设置再生一张，替换节点里的图':'生成（Ctrl+Enter）';
    $('cv-panel-error').hidden=node.run?.state!=='failed';
    $('cv-panel-error').textContent=node.run?.state==='failed'?node.run.error||'生成失败':'';
    if(popKind)renderPop();
  }
  function placePanel(metrics=null){
    if(!panelId||panel.hidden)return;
    const w=metrics?.wrapRect||wrap.getBoundingClientRect(),s=metrics?.hostRect||host.getBoundingClientRect();
    let rLeft,rWidth,rBottom;
    const cached=metrics?.boxById?.get(panelId);
    if(cached){
      const z=board.zoom;
      rLeft=s.left+(metrics.hostClientLeft??host.clientLeft)+board.canvas_x+cached.x*z;
      rWidth=cached.w*z;
      rBottom=s.top+(metrics.hostClientTop??host.clientTop)+board.canvas_y+(cached.y+cached.h)*z;
    }else{
      const el=element(panelId);if(!el)return closePanel();
      const r=el.getBoundingClientRect();
      rLeft=r.left;rWidth=r.width;rBottom=r.bottom;
    }
    const pWidth=metrics?.panelWidth||panel.offsetWidth;
    let left=rLeft+rWidth/2-pWidth/2-w.left;
    left=Math.max(s.left-w.left+12,Math.min(left,s.right-w.left-pWidth-12));
    panel.style.left=left+'px';panel.style.top=(rBottom-w.top+14)+'px';
  }
  function animateView(target,ms=300){
    const token=++viewAnim,from={x:board.canvas_x,y:board.canvas_y,zoom:board.zoom},start=performance.now();
    revealing=true;
    (function step(now){
      if(token!==viewAnim)return;
      const t=Math.min(1,(now-start)/ms),e=1-Math.pow(1-t,3);
      board.setView({x:from.x+(target.x-from.x)*e,y:from.y+(target.y-from.y)*e,zoom:from.zoom+(target.zoom-from.zoom)*e});
      if(t<1)requestAnimationFrame(step);else revealing=false;
    })(start);
  }
  // 单击选中节点：节点（生图节点连同下面的输入面板）居中放大到正好放满画面，最大 150%
  function focusNode(id){
    const box=boxes().find(b=>b.id===String(id));if(!box||!board)return;
    const s=host.getBoundingClientRect(),withPanel=String(id)===panelId&&!panel.hidden;
    const panelH=withPanel?panel.offsetHeight+14:0,label=30,top=24,bottom=96,side=48;
    const zoom=clampZoom(Math.min(1.5,(s.width-side*2)/box.w,(s.height-top-bottom-panelH)/(box.h+label)));
    const block=(box.h+label)*zoom+panelH,y0=top+Math.max(0,(s.height-top-bottom-block)/2);
    animateView({zoom,x:s.width/2-(box.x+box.w/2)*zoom,y:y0+label*zoom-box.y*zoom},320);
  }
  // 面板总在节点下面；下面放不下时，把画布往上平移一点（节点顶部不移出画面）
  function revealPanel(){
    const el=element(panelId);if(!el||panel.hidden||revealing||host.classList.contains('cv-dragging'))return;
    const r=el.getBoundingClientRect(),s=host.getBoundingClientRect();
    const overflow=r.bottom+14+panel.offsetHeight-(s.bottom-84);
    const shift=Math.min(overflow,Math.max(0,r.top-(s.top+16)));
    if(shift<=0)return;
    animateView({x:board.canvas_x,y:board.canvas_y-shift,zoom:board.zoom},220);
  }
  $('cv-prompt').addEventListener('input',()=>{const node=nodes.get(panelId);if(node){node.gen.prompt=$('cv-prompt').value;save(800);}});
  $('cv-prompt').addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();run(panelId);}});
  const mention=document.createElement('div');mention.className='cv-mention';mention.hidden=true;panel.append(mention);
  function mentionCandidates(query){
    const node=nodes.get(panelId);if(!node)return [];
    const linked=refItems(node.id).map((r,i)=>({...r,number:i+1}));        // 只列已经连上的参考图
    const q=query.replace(/\s+/g,'');
    return linked.filter(r=>!q||r.name.replace(/\s+/g,'').includes(q)||String(r.number)===q).slice(0,12);
  }
  function updateMention(){
    const ta=$('cv-prompt'),caret=ta.selectionStart;
    const found=ta.selectionStart===ta.selectionEnd&&ta.value.slice(0,caret).match(/@([^\s@，,。；;：:]{0,12})$/);
    const items=found?mentionCandidates(found[1]):[];
    if(!items.length)return closeMention();
    mentionState={start:caret-found[0].length,end:caret,items,index:Math.min(mentionState?.index||0,items.length-1)};
    renderMention();
  }
  function renderMention(){
    const {items,index}=mentionState;
    mention.innerHTML='<p>引用图片（↑↓ 选，回车确定）</p>'+items.map((r,i)=>
      `<button type="button" data-mention="${i}" class="${i===index?'active':''}"><img src="${thumbUrl(r.image.file,256)}" alt="">
        <span><b>图${r.number}</b>${escape(r.name)}</span></button>`).join('');
    mention.hidden=false;
    // 默认在提示词框下面；下面放不下（快到画布底边）就放到上面
    const ta=$('cv-prompt').getBoundingClientRect(),p=panel.getBoundingClientRect(),stage=host.getBoundingClientRect();
    const below=ta.bottom+4+mention.offsetHeight<=stage.bottom-8;
    mention.style.left=(ta.left-p.left)+'px';
    mention.style.top=(below?ta.bottom-p.top+4:ta.top-p.top-mention.offsetHeight-4)+'px';
  }
  function closeMention(){mentionState=null;mention.hidden=true;}
  function pickMention(index){
    const item=mentionState?.items[index],node=nodes.get(panelId);if(!item||!node)return;
    const ta=$('cv-prompt'),text=`@${item.name} `;
    ta.value=ta.value.slice(0,mentionState.start)+text+ta.value.slice(mentionState.end);
    const caret=mentionState.start+text.length;closeMention();
    ta.focus();ta.setSelectionRange(caret,caret);
    node.gen.prompt=ta.value;save(800);
  }
  $('cv-prompt').addEventListener('input',updateMention);
  $('cv-prompt').addEventListener('click',updateMention);
  $('cv-prompt').addEventListener('keyup',event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key))updateMention();});
  $('cv-prompt').addEventListener('blur',()=>setTimeout(()=>{if(document.activeElement!==$('cv-prompt'))closeMention();},150));
  $('cv-prompt').addEventListener('keydown',event=>{
    if(!mentionState)return;
    const count=mentionState.items.length;
    if(event.key==='ArrowDown'||event.key==='ArrowUp'){
      event.preventDefault();mentionState.index=(mentionState.index+(event.key==='ArrowDown'?1:count-1))%count;renderMention();
    }else if((event.key==='Enter'&&!event.ctrlKey&&!event.metaKey)||event.key==='Tab'){event.preventDefault();pickMention(mentionState.index);}
    else if(event.key==='Escape'){event.preventDefault();event.stopPropagation();closeMention();}
  });
  mention.addEventListener('mousedown',event=>event.preventDefault());      // 不让提示词框失去焦点
  mention.addEventListener('click',event=>{const b=event.target.closest('[data-mention]');if(b)pickMention(Number(b.dataset.mention));});
  $('cv-go').onclick=()=>run(panelId);
  $('cv-ref-row').addEventListener('click',event=>{
    if(!panelId)return;
    const skip=event.target.closest('[data-skip]');
    if(skip)return skipMember(skip.dataset.skip,panelId,skip.dataset.id);
    const button=event.target.closest('[data-unref]');if(!button)return;
    board.removeSingleConnection(button.dataset.unref,panelId,'output_1','input_1');
  });
  panel.addEventListener('click',event=>{
    const button=event.target.closest('[data-p]');if(!button||pop.contains(button))return;
    if(button.dataset.p==='upload')return $('cv-panel-file').click();
    togglePop(button.dataset.p);
  });
  $('cv-panel-file').onchange=guard(async()=>{
    const files=[...$('cv-panel-file').files];$('cv-panel-file').value='';
    if(!panelId||!files.length)return;
    const target=panelId,added=await uploadRaw(files);
    // 参考图放在生图节点左边，纵向排开、对齐格栅，并连上线
    const box=boxes().find(b=>b.id===target);
    const left={id:'_',x:box.x-NODE_WIDTH-GRID*4,y:box.y-GRID*2,w:NODE_WIDTH,h:0};
    const spots=stackBelow(left,added.map(f=>mediaHeight({results:[f]})),boxes(),GRID*2);
    added.forEach((file,i)=>{const id=create('upload',spots[i].x,spots[i].y,{results:[file],current:0});board.addConnection(id,target,'output_1','input_1');});
    setPicked([target]);save();
  });
  // 弹出的小面板：提示词预设、画布参考、参数
  function togglePop(kind){if(popKind===kind){closePop();return;}popKind=kind;renderPop();}
  function closePop(){popKind=null;pop.hidden=true;}
  function renderPop(){
    const node=nodes.get(panelId);if(!node||!popKind)return closePop();
    if(popKind==='presets'){
      const mine=prefs.presets||[];
      pop.innerHTML=`<p class="cv-pop-title">我的预设</p>
        <div class="cv-pop-items long">${mine.length?mine.map((t,i)=>`<span class="cv-mine"><button type="button" data-preset="${escape(t)}" title="${escape(t)}">${escape(t)}</button><button type="button" class="cv-mine-del" data-del="${i}" aria-label="删除这条预设" title="删除这条预设">${ICON.close}</button></span>`).join('')
          :'<p class="cv-pop-empty">还没有。写好提示词后点下面的按钮存起来，下次点一下就能用。</p>'}</div>
        <button type="button" class="cv-save-preset" data-savepreset ${node.gen.prompt.trim()?'':'disabled'}>＋ 把当前提示词存为预设</button>
        <p class="cv-pop-title">常用写法（点一下加进提示词）</p>
        <div class="cv-pop-items long">${BUILTIN_PRESETS.map(t=>`<button type="button" data-preset="${escape(t)}">${escape(t)}</button>`).join('')}</div>`;
    }else if(popKind==='refs'){
      const linked=new Set(refItems(node.id).map(r=>r.id));
      const candidates=[...nodes.values()].filter(n=>n.id!==node.id&&current(n));
      pop.innerHTML=`<p class="cv-pop-title">选画布上的图当参考（再点一次取消）</p>`+(candidates.length?`<div class="cv-pop-refs">${candidates.map(n=>
        `<button type="button" data-ref="${escape(n.id)}" class="${linked.has(n.id)?'active':''}"><img src="${thumbUrl(current(n).file,256)}" alt="" loading="lazy"><span>${escape(n.name)}</span></button>`).join('')}</div>`
        :'<p class="cv-pop-empty">画布上还没有别的图片。可以点「上传」，或把图片拖进画布。</p>');
    }else if(popKind==='channel'){
      const current=channelOf(node)?.id,list=settings?.channels||[];
      pop.innerHTML=`<p class="cv-pop-title">用哪个渠道生图（选了会记住，以后新建的节点也用它）</p>`+(list.length?`<div class="cv-pop-channels">${list.map(c=>
        `<button type="button" data-channel="${c.id}" class="${c.id===current?'active':''}"><span><b>${escape(c.name)}</b><small>${escape(c.model||'未选模型')}${c.key_source==='none'?' · 没有 Key':''}</small></span>${c.id===current?ICON.check:''}</button>`).join('')}</div>`
        :'<p class="cv-pop-empty">还没有生图渠道。</p>')+'<button type="button" class="cv-save-preset" data-manage>管理生图渠道…</button>';
    }else if(popKind==='params'){
      const g=node.gen,row=(label,key,values)=>`<div class="cv-pop-row"><span>${label}</span><div class="cv-seg">${values.map(([value,text])=>
        `<button type="button" data-set="${key}" data-value="${value}" class="${String(g[key])===String(value)?'active':''}">${text}</button>`).join('')}</div></div>`;
      pop.innerHTML=row('比例','ratio',RATIOS.map(r=>[r,`${ratioIcon(r)}${r}`]))+row('画质','quality',QUALITIES)+row('尺寸','resolution',SIZES.map(s=>[s,s]))
        +row('张数','count',[1,2,3,4].map(c=>[c,c+'张']))+'<p class="cv-pop-note">设好会记住，以后新建的图片节点都按这个。节点框按选的比例显示。比例会同时写进提示词，出图后差一点的会自动裁准。</p>';
    }
    pop.hidden=false;
    const anchor=panel.querySelector(`.cv-box [data-p="${popKind}"]`);
    const a=anchor.getBoundingClientRect(),p=panel.getBoundingClientRect(),s=host.getBoundingClientRect();
    pop.style.left=Math.max(0,Math.min(a.left-p.left,p.width-pop.offsetWidth))+'px';
    // 往哪边弹：参数默认往上、其余默认往下；那边放不下就换另一边
    const above=a.top-s.top-12,below=s.bottom-84-a.bottom,height=pop.offsetHeight+8;
    const up=['params','channel'].includes(popKind)?(above>=height||above>below):(below<height&&above>below);
    if(up){pop.style.top='auto';pop.style.bottom=(p.bottom-a.top+8)+'px';}
    else{pop.style.bottom='auto';pop.style.top=(a.bottom-p.top+8)+'px';}
  }
  pop.addEventListener('click',event=>{
    const node=nodes.get(panelId);if(!node)return;
    const preset=event.target.closest('[data-preset]'),del=event.target.closest('[data-del]'),savePreset=event.target.closest('[data-savepreset]');
    const ref=event.target.closest('[data-ref]'),set=event.target.closest('[data-set]');
    const pick=event.target.closest('[data-channel]'),manage=event.target.closest('[data-manage]');
    if(manage){const id=channelOf(node)?.id;closePop();channelsUI.open(id).catch(error=>toast(error.message));return;}
    if(pick){
      node.gen.channel=pick.dataset.channel;render(node.id);renderPanel();save();closePop();
      channelsUI.activate(pick.dataset.channel).catch(error=>toast(error.message));     // 记住：以后新建的节点都用它
      return;
    }
    if(del){const list=[...prefs.presets];list.splice(Number(del.dataset.del),1);savePrefs({presets:list});renderPop();}
    else if(savePreset){
      const text=node.gen.prompt.trim();if(!text)return;
      savePrefs({presets:[text,...prefs.presets.filter(p=>p!==text)].slice(0,60)});renderPop();toast('已存为预设');
    }else if(preset){node.gen.prompt=appendPrompt(node.gen.prompt,preset.dataset.preset);$('cv-prompt').value=node.gen.prompt;save();renderPop();}
    else if(ref){
      const via=refItems(node.id).find(r=>r.id===ref.dataset.ref&&r.via);
      if(via)return skipMember(via.via,node.id,via.id);
      const skipped=groups.find(g=>(g.links||[]).includes(node.id)&&g.skip?.[node.id]?.includes(ref.dataset.ref));
      if(skipped)return unskipMember(skipped.id,node.id,ref.dataset.ref);
      const linked=edges().some(e=>e.from===ref.dataset.ref&&e.to===node.id);
      if(linked)board.removeSingleConnection(ref.dataset.ref,node.id,'output_1','input_1');
      else board.addConnection(ref.dataset.ref,node.id,'output_1','input_1');
    }else if(set){
      const key=set.dataset.set,value=key==='count'?Number(set.dataset.value):set.dataset.value;
      node.gen[key]=value;savePrefs({gen:{...prefs.gen,[key]:value}});     // 记住：以后新建的节点都用这个
      render(node.id);renderPanel();save();
    }
  });
  document.addEventListener('pointerdown',event=>{
    if(!pop.hidden&&!pop.contains(event.target)&&!event.target.closest?.('.cv-box [data-p]'))closePop();
  },true);
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape'&&!pop.hidden){event.stopPropagation();closePop();}     // Esc 先关小面板，不取消选择
  },true);

  // ── 连线流动动效：只在已选中或「生图中」的连线上播放，静止连线零常驻动画 ──
  // 每条线后面依次叠：流动的亮线、看不见的粗线（好点中）。一定要排在主线后面：
  // Drawflow 移动节点时只更新连线的第一个子元素（children[0]），主线必须一直是第一个
  function syncActiveFlows(){
    if(!board)return;
    const isRunning=id=>nodes.get(String(id))?.run?.state==='running';
    for(const svg of host.querySelectorAll('svg.connection')){
      const cls=svg.getAttribute('class')||'';
      const from=cls.match(/node_out_node-(\S+)/)?.[1],to=cls.match(/node_in_node-(\S+)/)?.[1];
      const flowing=Boolean((from&&isRunning(from))||(to&&isRunning(to)));
      if(svg.classList.contains('is-flowing')!==flowing)svg.classList.toggle('is-flowing',flowing);
    }
    for(const flow of host.querySelectorAll('svg.cv-glinks .cv-flow')){
      const gid=flow.dataset.group,to=flow.dataset.to;
      const group=gid?groups.find(g=>g.id===gid):null;
      const flowing=Boolean((to&&isRunning(to))||group?.nodes.some(isRunning));
      if(flow.classList.contains('is-flowing')!==flowing)flow.classList.toggle('is-flowing',flowing);
    }
  }
  function flowFor(path){
    const d=path.getAttribute('d')||'';
    let flow=path.nextElementSibling;
    if(!flow?.classList?.contains('cv-flow')){flow=document.createElementNS(SVG_NS,'path');flow.setAttribute('class','cv-flow');path.after(flow);}
    flow.setAttribute('d',d);
    let hit=flow.nextElementSibling;
    if(!hit?.classList?.contains('cv-hit')){hit=document.createElementNS(SVG_NS,'path');hit.setAttribute('class','cv-hit');flow.after(hit);}
    hit.setAttribute('d',d);
  }
  function watchLines(){
    new MutationObserver(records=>{
      let addedConn=false;
      for(const record of records){
        if(record.type==='attributes'&&record.target.classList?.contains('main-path'))flowFor(record.target);
        for(const added of record.addedNodes||[])if(added.classList?.contains('connection')){added.querySelectorAll('.main-path').forEach(flowFor);addedConn=true;}
      }
      if(addedConn)syncActiveFlows();
    }).observe(board.precanvas,{subtree:true,childList:true,attributes:true,attributeFilter:['d']});
  }

  // ── 画布交互 ──
  function setup(){
    board=new Board(host);
    board.curvature=0.45;board.zoom_min=0.2;board.zoom_max=2.5;
    board.start();
    watchLines();
    board.on('nodePress',({id,event,still})=>{
      host.focus({preventScroll:true});
      if(event.shiftKey||event.ctrlKey||event.metaKey){
        const next=new Set(picked);next.has(id)?next.delete(id):next.add(id);setPicked([...next]);return;
      }
      if(still){if(!picked.has(id))setPicked([id]);return;}           // 点节点上的小按钮：只选中，不拖
      if(!picked.has(id)){setPicked([id]);beginMove([id],event,()=>focusNode(id));return;}
      // 按在已选中的一批里：拖动就整批一起动；没拖只是点一下，就只选这一个，并居中放大
      beginMove([...picked],event,()=>{if(picked.size>1)setPicked([id]);focusNode(id);});
    });
    board.on('marquee',event=>{host.focus({preventScroll:true});beginMarquee(event);});
    board.on('deleteKey',()=>remove([...picked],[...pickedLines]));
    board.on('connectionSelected',()=>setPicked([]));
    board.on('keydown',event=>{
      const mod=event.ctrlKey||event.metaKey,key=event.key.toLowerCase();
      if(event.key==='Escape')setPicked([]);
      else if(mod&&key==='g'){event.preventDefault();toggleGroup();}
      else if(mod&&key==='a'){event.preventDefault();setPicked([...nodes.keys()]);}
    });
    // 画布快捷键：焦点在画布外（比如刚点过工具栏按钮）也能用；画布自己有焦点时 Delete、Esc、Ctrl+G、Ctrl+A 由上面的 board 处理
    document.addEventListener('keydown',event=>{
      if(!active()||!board||event.defaultPrevented||event.target.closest?.('input,textarea,select,[contenteditable]')||document.querySelector('dialog[open]'))return;
      const mod=event.ctrlKey||event.metaKey,key=event.key.toLowerCase(),inBoard=host.contains(event.target);
      if(event.key==='Escape'&&!$('cv-keys-pop').hidden){showKeys(false);return;}
      if(event.key==='?'&&!mod){event.preventDefault();showKeys($('cv-keys-pop').hidden);return;}
      if(!inBoard&&!mod&&(event.key==='Delete'||event.key==='Backspace')){event.preventDefault();remove([...picked],[...pickedLines]);return;}
      if(!inBoard&&event.key==='Escape'){setPicked([]);return;}
      if(!mod||event.altKey)return;
      if(!inBoard&&key==='g'){event.preventDefault();toggleGroup();}
      else if(!inBoard&&key==='a'){event.preventDefault();setPicked([...nodes.keys()]);}
      else if(key==='z'&&!event.shiftKey){event.preventDefault();undo();}
      else if((key==='z'&&event.shiftKey)||key==='y'){event.preventDefault();redo();}
      else if(key==='d'){event.preventDefault();duplicate();}
    });
    $('cv-undo').onclick=()=>undo();$('cv-redo').onclick=()=>redo();
    $('cv-keys').onclick=()=>showKeys($('cv-keys-pop').hidden);
    document.addEventListener('pointerdown',event=>{
      if(!$('cv-keys-pop').hidden&&!event.target.closest('#cv-keys-pop,#cv-keys'))showKeys(false);
    });
    board.on('connectionCreated',({input_id})=>{render(input_id);if(panelId===String(input_id))renderPanel();save();});
    board.on('connectionRemoved',({input_id})=>{render(input_id);if(panelId===String(input_id))renderPanel();save();markLines();});
    board.on('connectionStart',({output_id})=>{pendingConnection={from:output_id,cancelled:false};});
    board.on('connectionCancel',()=>{if(pendingConnection)pendingConnection.cancelled=true;});
    board.on('mouseUp',event=>{
      // 从「+」拖线到空白处松手：在那里新建一个生图节点，并把线连上（LibTV 的做法）
      const pending=pendingConnection;pendingConnection=null;
      if(!pending?.cancelled||!isEmpty(event?.target))return;
      const point=worldAt(event.clientX,event.clientY);
      newGenAt(point.x,point.y-90,pending.from);
    });
    board.on('translate',()=>{background();placePanel();placeSelection();clearTimeout(viewTimer);viewTimer=setTimeout(()=>save(0),1200);});
    board.on('zoom',zoom=>{$('cv-zoom').textContent=Math.round(zoom*100)+'%';host.style.setProperty('--cv-hit',(16/zoom).toFixed(1)+'px');});   // 连线的点击区在屏幕上始终约 16px
    host.addEventListener('dblclick',event=>{
      if(event.target.closest('.cv-media img')){viewNode(nodes.get(event.target.closest('.drawflow-node').id.slice(5)));return;}
      if(!isEmpty(event.target))return;
      const point=worldAt(event.clientX,event.clientY);
      newGenAt(point.x-NODE_WIDTH/2,point.y-90);
    });
    host.addEventListener('auxclick',event=>{if(event.button===1)event.preventDefault();});
    // 中键在画布外松开时，Drawflow 收不到 mouseup，这里补上
    window.addEventListener('mouseup',event=>{if(event.button===1&&board.editor_selected&&!host.contains(event.target))board.dragEnd(event);});
    host.addEventListener('dragover',event=>{lastDrop={x:event.clientX,y:event.clientY};});
    window.addEventListener('resize',()=>{placePanel();placeSelection();});
    document.addEventListener('visibilitychange',()=>{
      if(document.hidden){stopPollTimers();return;}
      if(active()&&runningNodes().length){updateProgressText();poll();}
    });
  }
  // 画布底色是纯色（不画格栅点），这里只管空画布提示
  function background(){$('canvas-empty').hidden=nodes.size>0;}
  function showKeys(open){$('cv-keys-pop').hidden=!open;$('cv-keys').setAttribute('aria-expanded',String(open));}

  // ── 载入一张画布（顶栏切换时也走这里） ──
  async function openCanvas(project,canvasId){
    if(pid)await flush();
    const saved=await api(`/api/canvas/projects/${project.id}/canvases/${canvasId}/board`);
    closePanel();setPicked([]);
    pid=project.id;cid=canvasId;
    steps=[];historyAt=-1;genCache.clear();clearTimeout(commitTimer);
    batching+=1;
    let split=false;
    try{
      loadBoard(saved);
      // 旧版（1.5.0）一个节点里存了多张：拆成各自的节点，纵向排开
      for(const node of [...nodes.values()]){
        if(node.results.length>1){
          const keep=current(node),rest=node.results.filter(r=>r!==keep);
          node.results=[keep];node.current=0;render(node.id);
          stackResults(node,rest);split=true;
        }
      }
      renderGroups();
    }finally{batching-=1;}
    if(saved.view&&nodes.size)board.setView(saved.view);else fit();
    background();
    schedulePoll();
    if(split)save(0);else commit();
  }
  // 把一份画布（存盘的、或撤销历史里的）装进 Drawflow，节点都重画一遍；视角不动
  function loadBoard(saved){
    nodes=new Map();seq=saved.seq||0;groups=Array.isArray(saved.groups)?saved.groups.map(g=>({...g,nodes:(g.nodes||[]).map(String),links:(g.links||[]).map(String),
      skip:Object.fromEntries(Object.entries(g.skip&&typeof g.skip==='object'?g.skip:{}).map(([to,ids])=>[String(to),(Array.isArray(ids)?ids:[]).map(String)]))})):[];
    const drawflow={drawflow:{Home:{data:{}}}},links=saved.edges||[];
    for(const item of saved.nodes||[]){
      const node={...item,id:String(item.id),gen:{prompt:'',...prefs.gen,...(item.gen||{})},results:item.results||[]};
      delete node.x;delete node.y;
      nodes.set(node.id,node);
      drawflow.drawflow.Home.data[node.id]={id:Number(node.id),name:'image',data:{},class:CLASS[node.kind]||CLASS.gen,html:'',typenode:false,
        pos_x:item.x||0,pos_y:item.y||0,
        inputs:node.kind==='gen'?{input_1:{connections:links.filter(e=>String(e.to)===node.id).map(e=>({node:String(e.from),input:'output_1'}))}}:{},
        outputs:{output_1:{connections:links.filter(e=>String(e.from)===node.id).map(e=>({node:String(e.to),output:'input_1'}))}}};
    }
    for(const entry of Object.values(drawflow.drawflow.Home.data)){
      for(const port of Object.values(entry.inputs))port.connections=port.connections.filter(c=>drawflow.drawflow.Home.data[c.node]);
      for(const port of Object.values(entry.outputs))port.connections=port.connections.filter(c=>drawflow.drawflow.Home.data[c.node]?.inputs?.input_1);
    }
    board.import(drawflow,false);
    renderAll();
  }
  function fit(){
    const rect=host.getBoundingClientRect();
    board.setView(fitView(boxes(),rect.width,rect.height));
  }

  // ── 底部工具条 ──
  // 「＋ 图片节点」：放在画面中间；中间有节点挡着就往右挪到空位，不叠在别的节点上
  $('cv-add').onclick=()=>{
    const c=viewportCenter(),h=mediaHeight({kind:'gen',gen:prefs.gen,results:[]}),all=boxes();
    let spot=snapPoint({x:c.x-NODE_WIDTH/2,y:c.y-140});
    for(let i=0;i<30&&all.some(b=>spot.x<b.x+b.w+GRID&&b.x<spot.x+NODE_WIDTH+GRID&&spot.y<b.y+b.h+GRID*2&&b.y<spot.y+h+GRID*2);i++)
      spot={x:spot.x+NODE_WIDTH+GRID*2,y:spot.y};
    newGenAt(spot.x,spot.y);
  };
  $('cv-upload').onclick=()=>$('cv-file').click();
  $('cv-file').onchange=guard(async()=>{const files=[...$('cv-file').files];$('cv-file').value='';await uploadFiles(files);});
  $('cv-zoom-in').onclick=()=>board.zoomTo(board.zoom*1.2);
  $('cv-zoom-out').onclick=()=>board.zoomTo(board.zoom/1.2);
  $('cv-zoom').onclick=()=>board.zoomTo(1);
  $('cv-fit').onclick=fit;
  $('cv-empty-add').onclick=()=>$('cv-add').click();
  $('cv-empty-upload').onclick=()=>$('cv-file').click();

  async function uploadRaw(files){
    const images=files.filter(f=>/^image\/(png|jpe?g|webp|gif|bmp)$/.test(f.type));
    if(!images.length)throw new Error('只接受 jpg、png、webp、gif、bmp 图片');
    const added=[];
    for(const file of images){
      const response=await fetch(`/api/canvas/projects/${pid}/files`,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});
      const result=await response.json().catch(()=>null);
      if(!response.ok||!result)throw new Error(result?.error||'图片上传失败');
      added.push(result);
    }
    return added;
  }
  async function uploadFiles(files,point){addImages(await uploadRaw(files),point);}
  function dropPoint(clientX,clientY){
    if(clientX===undefined){if(!lastDrop)return null;({x:clientX,y:clientY}=lastDrop);}
    return worldAt(clientX,clientY);
  }
  // ── 复制 / 粘贴节点（Ctrl+C / Ctrl+V，Ctrl+D 原地复制一份）──
  // 复制时往系统剪贴板写一行标记：之后又去别处复制了截图，Ctrl+V 就粘截图；剪贴板还是这行标记，就粘节点。
  // 带上提示词、设置和图；选中节点之间的连线、整组都选中的分组一起复制；同一张画布里粘贴时，从外面连进来的参考图也连上。
  let copied=null;
  const editing=target=>!!target?.closest?.('input,textarea,select,[contenteditable]');
  function copyData(ids){
    const set=new Set(ids.map(String).filter(id=>nodes.has(id)));
    if(!set.size)return null;
    const all=edges();
    return {pid,cid,times:0,
      nodes:[...set].map(id=>({node:structuredClone(nodes.get(id)),x:data()[id]?.pos_x??0,y:data()[id]?.pos_y??0})),
      edges:all.filter(e=>set.has(e.from)&&set.has(e.to)),
      inbound:all.filter(e=>!set.has(e.from)&&set.has(e.to)),
      links:groups.flatMap(g=>(g.links||[]).filter(to=>set.has(to)&&!g.nodes.some(id=>set.has(id))).map(to=>({group:g.id,to,skip:g.skip?.[to]||null}))),
      groups:groups.filter(g=>g.nodes.length>1&&g.nodes.every(id=>set.has(id))).map(g=>structuredClone(g))};
  }
  async function pasteData(clip,offset){
    if(!clip?.nodes.length)return;
    const files=[...new Set(clip.nodes.flatMap(({node})=>(node.results||[]).map(r=>r.file)))];
    if(clip.pid!==pid&&files.length)await api(`/api/canvas/projects/${pid}/files/copy`,{from:clip.pid,files});
    const same=clip.pid===pid&&clip.cid===cid;
    const box=boundsOf(clip.nodes.map(({node,x,y})=>({x,y,w:NODE_WIDTH,h:mediaHeight(node)})));
    const c=viewportCenter();
    const dx=same?offset:snap(c.x-(box.x+box.w/2)),dy=same?offset:snap(c.y-(box.y+box.h/2));
    const map=new Map();
    batch(()=>{
      for(const {node,x,y} of clip.nodes){
        const {id:_old,...rest}=structuredClone(node);
        if(rest.run?.state==='running')rest.run=null;           // 还在生成的：只复制提示词和设置
        map.set(node.id,create(node.kind,x+dx,y+dy,rest));
      }
      for(const e of clip.edges)board.addConnection(map.get(e.from),map.get(e.to),'output_1','input_1');
      if(same){
        for(const e of clip.inbound)if(nodes.has(e.from))board.addConnection(e.from,map.get(e.to),'output_1','input_1');
        for(const l of clip.links){
          const g=groups.find(x=>x.id===l.group);if(!g)continue;
          g.links=[...(g.links||[]),map.get(l.to)];
          if(l.skip?.length)g.skip={...(g.skip||{}),[map.get(l.to)]:l.skip};
        }
      }
      let number=Math.max(0,...groups.map(g=>Number(String(g.name).match(/\d+/)?.[0])||0));
      for(const g of clip.groups){
        const links=(g.links||[]).filter(to=>map.has(to)).map(to=>map.get(to));
        groups.push({id:'g'+Date.now().toString(36)+Math.random().toString(36).slice(2,6),name:`分组 ${++number}`,
          nodes:g.nodes.map(id=>map.get(id)),links,
          skip:Object.fromEntries(Object.entries(g.skip||{}).filter(([to])=>map.has(to)).map(([to,ids])=>[map.get(to),ids.map(id=>map.get(id)).filter(Boolean)]))});
      }
      renderGroups();
    });
    for(const id of map.values())render(id);
    const ids=[...map.values()];
    setPicked(ids);save(0);
    requestAnimationFrame(()=>showAll(ids));
  }
  function duplicate(){
    if(!picked.size)return;
    pasteData(copyData([...picked]),GRID*2).catch(error=>toast(error.message));
  }
  document.addEventListener('copy',event=>{
    if(!active()||!ready||!picked.size||editing(document.activeElement)||String(window.getSelection?.()||''))return;
    const clip=copyData([...picked]);if(!clip)return;
    clip.marker=`Panda Studio 画布节点 ×${clip.nodes.length}（${Date.now().toString(36)}）`;
    copied=clip;
    event.preventDefault();event.clipboardData.setData('text/plain',clip.marker);
    toast(`已复制 ${clip.nodes.length} 个节点，Ctrl+V 粘贴`);
  });
  document.addEventListener('paste',event=>{
    if(!active()||!ready||editing(document.activeElement))return;
    const text=event.clipboardData?.getData('text/plain')||'';
    if(copied&&text===copied.marker){
      event.preventDefault();copied.times+=1;
      pasteData(copied,GRID*2*copied.times).catch(error=>toast(error.message));
      return;
    }
    const files=[...(event.clipboardData?.files||[])];
    if(files.length){event.preventDefault();uploadFiles(files).catch(error=>toast(error.message));}
  });

  // ── 生图渠道（弹窗在 web/canvas-channels.js） ──
  // 节点用自己选的渠道；没选过或那个渠道删了，就用当前渠道
  function channelOf(node){
    const list=settings?.channels||[];
    return list.find(c=>c.id===node?.gen?.channel)||list.find(c=>c.id===settings?.active)||null;
  }
  const channelLabel=c=>c.model?`${c.name} · ${c.model}`:`${c.name}（未选模型）`;
  function settingsPill(){
    const pill=$('cv-api'),current=(settings?.channels||[]).find(c=>c.id===settings?.active);
    const ok=current?.model&&current.key_source!=='none'&&(current.base_url||settings.ai_base_url);
    pill.dataset.state=ok?'ok':'missing';
    pill.textContent=current?channelLabel(current):'先配置生图渠道';
    pill.title=current?'当前生图渠道（点开管理渠道）':'点开配置生图渠道';
    if(panelId)renderPanel();
  }
  const readSettings=()=>channelsUI.read();
  const openSettings=()=>channelsUI.open(settings?.active).catch(error=>toast(error.message));
  $('cv-settings').onclick=openSettings;
  $('cv-api').onclick=openSettings;

  // ── 页面 ──
  function active(){return !$('canvas-page').hidden;}
  async function show(){
    if(!board)setup();
    readSettings().catch(()=>{});
    if(!ready){
      try{
        prefs={...prefs,...(await api('/api/canvas/prefs'))};
        await projectsUI.init();
        ready=true;
      }catch(error){toast('画布打不开：'+error.message);}
      return;
    }
    background();schedulePoll();placePanel();placeSelection();
  }
  async function needReady(){if(!ready)throw new Error('画布还没打开');}
  return {show,
    hide(){flush();stopPollTimers();closePop();showKeys(false);},
    retryPoll(){runPollFailures=0;if(active()&&runningNodes().length)poll();},
    async flush(){if(board)await Promise.all([flush(),flushPrefs()]);},
    active,
    async addPaths(paths){
      await needReady();
      if(!paths?.length)return toast('只接受 jpg、png、webp、gif、bmp 图片');
      const {files}=await api(`/api/canvas/projects/${pid}/files/paths`,{paths});addImages(files,dropPoint());
    },
    async dropFiles(files,clientX,clientY){await needReady();return uploadFiles(files,dropPoint(clientX,clientY));}};
}
