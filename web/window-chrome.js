// 无边框桌面窗口的外框：窗口按钮、拖动区域、边缘拉伸、四周大圆角。
// 只在 Panda Studio 的桌面窗口里生效（pywebview 提供 window_* 接口时）；在浏览器里打开不出现。
// 拖动：只认带 win-drag 标记的元素本身，标题栏里的按钮照常点击，不会带动窗口。
// 按下时交给系统移动窗口（desktop.pyw 的 window_start_drag），这样能拖到屏幕边上分屏、Win+方向键也能用；
// 不再用 pywebview 自带的网页里挪窗口（那种拖法不会触发系统分屏）。
const root=document.documentElement;
const api=()=>window.pywebview?.api;
let maximized=false;

const EDGES=['n','s','e','w','ne','nw','se','sw'];
const DRAG='.page-header,.page-title,.page-title h1,.page-header-inline,.task-header,.rail,.cv-topbar,.cv-topbar-space';   // 画布顶栏的空白处也能拖动窗口

function setMaximized(value){
  maximized=!!value;
  root.classList.toggle('maximized',maximized);
  const button=document.querySelector('.window-controls [data-win="max"]');
  if(button){const label=maximized?'还原':'最大化';button.title=label;button.setAttribute('aria-label',label);}
}

async function toggleMaximize(){setMaximized(await api().window_toggle_maximize());}

// 关窗口前先把还没存的改动存掉（画布、任务页的改动都是停一下才存），最多等 3 秒。
// 各页面往 window.__whitebgBeforeClose 里放自己的「立刻保存」。Alt+F4、任务栏关闭由 desktop.pyw 转到 __whitebgCloseSoon。
async function closeWindow(){
  const hooks=window.__whitebgBeforeClose||[];
  try{await Promise.race([Promise.allSettled(hooks.map(fn=>Promise.resolve().then(fn))),new Promise(resolve=>setTimeout(resolve,3000))]);}
  finally{api().window_close();}
}
window.__whitebgCloseSoon=()=>{closeWindow();};

function setup(){
  const bridge=api();
  if(!bridge?.window_minimize||root.classList.contains('frameless'))return;
  root.classList.add('frameless');
  document.body.insertAdjacentHTML('beforeend',`
    <div class="window-controls" role="group" aria-label="窗口">
      <button type="button" data-win="min" title="最小化" aria-label="最小化"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 12h11"/></svg></button>
      <button type="button" data-win="max" title="最大化" aria-label="最大化"><svg class="i-max" viewBox="0 0 24 24" aria-hidden="true"><rect x="6.5" y="6.5" width="11" height="11" rx="2.6"/></svg><svg class="i-restore" viewBox="0 0 24 24" aria-hidden="true"><rect x="5.5" y="9" width="9.5" height="9.5" rx="2.2"/><path d="M9 9V7.6A2.1 2.1 0 0 1 11.1 5.5h5.3a2.1 2.1 0 0 1 2.1 2.1v5.3a2.1 2.1 0 0 1-2.1 2.1H15"/></svg></button>
      <button type="button" data-win="close" title="关闭" aria-label="关闭"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7.5 7.5 9 9m0-9-9 9"/></svg></button>
    </div>
    <div class="window-drag-strip win-drag" aria-hidden="true"></div>
    ${EDGES.map(edge=>`<div class="window-edge" data-edge="${edge}" aria-hidden="true"></div>`).join('')}`);
  for(const element of document.querySelectorAll(DRAG))element.classList.add('win-drag');

  document.querySelector('.window-controls').onclick=event=>{
    const kind=event.target.closest('[data-win]')?.dataset.win;
    if(kind==='min')bridge.window_minimize();
    else if(kind==='max')toggleMaximize();
    else if(kind==='close')closeWindow();
  };
  // 按住标题区：交给系统拖动。双击（两次按下离得近、间隔短）切换最大化。
  // 系统接管拖动后网页收不到松开和双击事件，所以双击自己判断。
  let lastDown={time:0,x:0,y:0};
  document.addEventListener('mousedown',event=>{
    if(event.button!==0||!event.target.classList?.contains('win-drag'))return;
    const now=performance.now(),near=Math.abs(event.screenX-lastDown.x)<6&&Math.abs(event.screenY-lastDown.y)<6;
    if(now-lastDown.time<450&&near){lastDown={time:0,x:0,y:0};toggleMaximize();return;}
    lastDown={time:now,x:event.screenX,y:event.screenY};
    bridge.window_start_drag?.();
  });
  // 贴边分屏、Win+↑↓、拖动还原都会改变最大化状态：窗口大小变了就问一下现在是不是最大化（换图标、去掉圆角描边）
  let sizeTimer;
  window.addEventListener('resize',()=>{clearTimeout(sizeTimer);sizeTimer=setTimeout(()=>bridge.window_maximized?.().then(setMaximized,()=>{}),120);});

  // 边缘拉伸：按住边缘拖动，按鼠标位移算新尺寸，交给 pywebview 改窗口大小（拉哪边固定对边）
  let drag=null,sending=false,pending=null;
  async function flush(){
    if(sending||!pending)return;
    sending=true;const {width,height,edge}=pending;pending=null;
    try{await bridge.window_resize(width,height,edge);}finally{sending=false;if(pending)flush();}
  }
  for(const handle of document.querySelectorAll('.window-edge')){
    handle.onpointerdown=event=>{
      if(maximized||event.button!==0)return;
      event.preventDefault();handle.setPointerCapture(event.pointerId);
      drag={id:event.pointerId,edge:handle.dataset.edge,x:event.screenX,y:event.screenY,w:window.outerWidth,h:window.outerHeight};
    };
    handle.onpointermove=event=>{
      if(drag?.id!==event.pointerId)return;
      const dx=event.screenX-drag.x,dy=event.screenY-drag.y,edge=drag.edge;
      const width=drag.w+(edge.includes('e')?dx:edge.includes('w')?-dx:0);
      const height=drag.h+(edge.includes('s')?dy:edge.includes('n')?-dy:0);
      pending={width:Math.max(900,Math.round(width)),height:Math.max(650,Math.round(height)),edge};
      flush();
    };
    handle.onpointerup=handle.onpointercancel=event=>{if(drag?.id===event.pointerId)drag=null;};
  }
  bridge.window_maximized?.().then(setMaximized,()=>{});
}

if(api()?.window_minimize)setup();
window.addEventListener('pywebviewready',setup);
