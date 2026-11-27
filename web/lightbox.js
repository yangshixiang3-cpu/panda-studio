import {copyImage} from './clipboard.js';
// 比例按原图像素计算，平移以显示区域中心为原点。
export const clampScale=(scale,fit)=>Math.max(fit,Math.min(8,scale));
export function zoomAt({scale,x,y},nextScale,point){
  const ratio=nextScale/scale;
  return {scale:nextScale,x:point.x-(point.x-x)*ratio,y:point.y-(point.y-y)*ratio};
}
export const pageIndex=(index,delta,length)=>Math.max(0,Math.min(Math.max(0,length-1),index+delta));
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const previewAttributes=(src,caption,group)=>`data-preview="${escape(src)}" data-caption="${escape(caption)}" data-preview-group="${escape(group)}" tabindex="0" aria-label="放大：${escape(caption)}"`;
let box;
export function openLightbox(items,startIndex=0){
  if(!items?.length)return;
  box??=createLightbox();box.open(items,startIndex);
}
function createLightbox(){
  const dialog=document.createElement('dialog');dialog.className='lightbox';
  dialog.setAttribute('aria-label','图片放大预览');
  dialog.innerHTML=`<div class="lightbox-toolbar"><output aria-label="当前比例"></output><button data-fit>适应窗口</button><button data-original>1:1</button><button data-copy title="复制这张图，可以直接粘贴到微信、PS 等">复制图片</button><button data-close aria-label="关闭图片预览">×</button></div><div class="lightbox-stage"><p class="lightbox-status" role="status"></p></div><button class="lightbox-prev" aria-label="上一张">‹</button><button class="lightbox-next" aria-label="下一张">›</button><div class="lightbox-caption" aria-live="polite"></div>`;
  document.body.append(dialog);
  const stage=dialog.querySelector('.lightbox-stage'),status=dialog.querySelector('.lightbox-status'),caption=dialog.querySelector('.lightbox-caption'),ratio=dialog.querySelector('output'),prev=dialog.querySelector('.lightbox-prev'),next=dialog.querySelector('.lightbox-next');
  let items=[],index=0,img,fit=1,pose={scale:1,x:0,y:0},drag=null,opener,overflow,htmlOverflow,ready=false;
  function draw(){
    if(img)img.style.transform=`translate(-50%, -50%) translate(${pose.x}px, ${pose.y}px) scale(${pose.scale})`;
    ratio.textContent=ready?`${Math.round(pose.scale*100)}%`:'—';
    stage.classList.toggle('zoomed',ready&&pose.scale>fit);
  }
  function reset(original=false){if(!ready)return;pose={scale:original?1:fit,x:0,y:0};draw();}
  function measure(){if(!ready)return;fit=Math.min(1,stage.clientWidth/img.naturalWidth,stage.clientHeight/img.naturalHeight);reset();}
  function show(){
    drag=null;ready=false;img?.remove();status.hidden=false;status.textContent='加载中…';
    const current=items[index];caption.textContent=current.caption||'';
    prev.hidden=next.hidden=items.length<2;prev.disabled=index===0;next.disabled=index===items.length-1;
    const loading=new Image();img=loading;loading.alt=current.caption||'图片预览';loading.draggable=false;loading.hidden=true;
    loading.onload=()=>{if(img!==loading||!dialog.open)return;ready=true;loading.hidden=false;status.hidden=true;caption.textContent=`${current.caption||''} · ${loading.naturalWidth} × ${loading.naturalHeight} 像素`;measure();};
    loading.onerror=()=>{if(img!==loading||!dialog.open)return;status.textContent='图片打不开';};
    stage.append(loading);draw();loading.src=current.src;
  }
  function turn(delta){const value=pageIndex(index,delta,items.length);if(value!==index){index=value;show();}}
  function close(){if(!dialog.open)return;dialog.close();}
  dialog.addEventListener('close',()=>{
    drag=null;document.body.style.overflow=overflow;document.documentElement.style.overflow=htmlOverflow;
    if(opener?.isConnected)opener.focus({preventScroll:true});
    else if(opener?.dataset.preview){const replacement=[...document.querySelectorAll('[data-preview]')].find(e=>e.dataset.preview===opener.dataset.preview&&e.dataset.previewGroup===opener.dataset.previewGroup);replacement?.focus({preventScroll:true});}
  });
  dialog.addEventListener('cancel',e=>{e.preventDefault();close();});
  dialog.querySelector('[data-close]').onclick=close;
  dialog.querySelector('[data-fit]').onclick=()=>reset();dialog.querySelector('[data-original]').onclick=()=>reset(true);
  prev.onclick=()=>turn(-1);next.onclick=()=>turn(1);
  const copyButton=dialog.querySelector('[data-copy]');let copyTimer;
  copyButton.onclick=async()=>{
    const label='复制图片';clearTimeout(copyTimer);copyButton.disabled=true;copyButton.title='';
    try{await copyImage(items[index].src);copyButton.textContent='已复制';}
    catch(error){copyButton.textContent='复制失败';copyButton.title=error.message;}
    finally{copyButton.disabled=false;copyTimer=setTimeout(()=>{copyButton.textContent=label;},1800);}
  };
  let moved=false;
  dialog.addEventListener('click',e=>{if(!moved&&(e.target===stage||e.target===dialog))close();moved=false;});
  dialog.addEventListener('keydown',e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();turn(e.key==='ArrowLeft'?-1:1);}});
  stage.addEventListener('wheel',e=>{
    e.preventDefault();if(!ready)return;
    const rect=stage.getBoundingClientRect(),scale=clampScale(pose.scale*Math.exp(-e.deltaY*.002),fit);
    pose=zoomAt(pose,scale,{x:e.clientX-rect.left-rect.width/2,y:e.clientY-rect.top-rect.height/2});
    if(scale===fit)pose.x=pose.y=0;draw();
  },{passive:false});
  stage.addEventListener('dblclick',e=>{e.preventDefault();reset(Math.abs(pose.scale-fit)<.0001);});
  stage.addEventListener('pointerdown',e=>{moved=false;if(e.button!==0||!ready||pose.scale<=fit||e.target!==img)return;drag={id:e.pointerId,x:e.clientX,y:e.clientY,pose:{...pose}};img.setPointerCapture(e.pointerId);});
  stage.addEventListener('pointermove',e=>{if(!drag||drag.id!==e.pointerId)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;moved ||= Math.abs(dx)+Math.abs(dy)>3;pose={...pose,x:drag.pose.x+dx,y:drag.pose.y+dy};draw();});
  const release=()=>{drag=null;};stage.addEventListener('pointerup',release);stage.addEventListener('pointercancel',release);stage.addEventListener('lostpointercapture',release);
  window.addEventListener('resize',()=>{if(dialog.open)measure();});
  window.addEventListener('hashchange',close);
  return {open(values,start){
    if(!dialog.open){opener=document.activeElement;overflow=document.body.style.overflow;htmlOverflow=document.documentElement.style.overflow;document.body.style.overflow='hidden';document.documentElement.style.overflow='hidden';dialog.showModal();}
    items=values.slice();index=pageIndex(Number.isInteger(start)?start:0,0,items.length);show();dialog.querySelector('[data-close]').focus();
  }};
}
export function installLightbox(){
  // 捕获阶段拦住缩略图点击，避免触发行选择或参考图选择。
  function activate(event){
    const target=event.target.closest('[data-preview]');if(!target)return;
    if(event.type==='keydown'&&!['Enter',' '].includes(event.key))return;
    event.preventDefault();event.stopImmediatePropagation();target.focus({preventScroll:true});
    const group=target.dataset.previewGroup;
    const nodes=group?[...document.querySelectorAll('[data-preview]')].filter(n=>n.dataset.previewGroup===group):[target];
    openLightbox(nodes.map(n=>({src:n.dataset.preview,caption:n.dataset.caption})),nodes.indexOf(target));
  }
  document.addEventListener('click',activate,true);document.addEventListener('keydown',activate,true);
}
