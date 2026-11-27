import {installLightbox,previewAttributes} from './lightbox.js';
installLightbox();
import {installLightingUI,effectiveLighting} from './lighting.js';
import {Viewer} from './viewer.js';
import {installCalibrationUI} from './calibration.js';
import {installMaterialsUI} from './materials.js';
import {installAISettings} from './ai-settings.js';
import {RAD,clamp,toSpherical,fromSpherical,fitPose,focalLengthMm,fovFromFocal,basis,projectedOrientation,canCorrectVertically,describePose,verticalProjection} from './camera.js';
import {syncRange} from './range.js';
import {installRunner} from './runner.js';
import {installSettings} from './settings.js';
import {installCanvas} from './canvas.js';
import {bundleSignature,hasRunningItems,pollDelay,renderModes,renumberItems,rowStatus,syncBackgroundPause,taskSteps} from './task-state.js';
import {detectVariants,matchVariants,variantPayload} from './variants.js';
import {api,backoffDelay,buildFileUrl,fetchWithTimeout,setOfflineRetry,uploadBlob} from './api.js';

const $=id=>document.getElementById(id),escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const base=p=>p.replaceAll('\\','/').split('/').pop();
let scan=null,task=null,selected=0,viewer=null,modelKey='',reference='',saveTimer=null,dirty=false,revision=0,saving=Promise.resolve(),loading=false,polling=null,dragNo=null;
let lastBundle=null,bundleKey=null,appliedKey=null,pollTimer,pollAbort=null,pollFailures=0,flushAbort=null,routeVersion=0,frameRequest,updateLighting=true;
let desktop=false,pendingReferences=[],referenceBusy=false,inputQueue=Promise.resolve();
const fileUrl=(path,id=task?.id,w=0)=>buildFileUrl(path,id,w);
function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').hidden=true,5000);}
function guard(fn){return async event=>{try{await fn(event);}catch(error){toast(error.message);}};}
function modal(title,content){$('dialog').dataset.view='';$('dialog-title').textContent=title;$('dialog-content').innerHTML=content;if(!$('dialog').open)$('dialog').showModal();}
$('dialog-close').onclick=()=>$('dialog').close();
function item(){return task?.items[selected];}
function outputPath(entry,key='file'){
  const value=entry[key];if(!value)return '';
  if(/^[a-z]:|^[/\\]/i.test(value))return value;
  // preview_fbx 为绝对路径；由服务端返回的任务目录生成相对成果路径。
  return `${lastBundle.task_dir}/${value}`;
}
const storageSize=bytes=>new Intl.NumberFormat('zh-CN',{maximumFractionDigits:2}).format(bytes/1024**2)+' MB';
let taskListVersion=0;
function renderStorage(data){
  $('storage-size').textContent='交换目录 '+storageSize(data.exchange_bytes);
  $('storage-cleanup').hidden=!data.cleanable_tasks;
}
// 首页任务卡：渐变卡片轮换（橄榄 / 玫瑰 / 橄榄棕 / 白），进度弧 + 大号成品数 + 点阵
function taskCard(t,index){
  const when=String(t.created_at||'').replace('T',' ').slice(5,16);
  const status=t.cleaned_at?['cleaned','已清理']:t.delivered?['done','已入库']:['active','进行中'];
  const total=t.item_count||0,done=t.final_done||0,shots=t.smallshot_done||0;
  const pct=total?Math.round(done/total*100):0;
  const dots=Array.from({length:Math.min(Math.max(total,1),12)},(_,k)=>`<i class="${k<done?'on':k<shots?'half':''}"></i>`).join('');
  const thumb=t.thumb?`<span class="tc-thumb"><img src="${escape(fileUrl(t.thumb,t.id,360))}" alt="" loading="lazy" decoding="async"></span>`:'';
  const name=t.product_name+(t.variant_name&&t.variant_count>1?' · '+t.variant_name:'');
  return `<div class="task-card-wrap" data-wrap="${escape(t.id)}"><button class="tc-delete" type="button" data-delete="${escape(t.id)}" data-name="${escape(name)}" title="删除这个任务（可撤销）" aria-label="删除任务 ${escape(name)}">`
    +`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 7h15M10 11v6M14 11v6M6.5 7l.9 11.2A2 2 0 0 0 9.4 20h5.2a2 2 0 0 0 2-1.8L17.5 7M9.5 7V5.2A1.2 1.2 0 0 1 10.7 4h2.6a1.2 1.2 0 0 1 1.2 1.2V7"/></svg></button>`
    +`<button class="task-card tone-${index%4}" data-task="${escape(t.id)}" title="任务 ${escape(t.id)}">`
    +`<span class="tc-head"><span class="tc-ring" style="--p:${pct}"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="4"/><circle cx="15.5" cy="8.5" r="1.5"/><path d="m4.5 17 4.5-4.5 3 3 2-2 5.5 5.5"/></svg></span>`
    +`<span class="tc-title"><strong>${escape(t.product_name)}</strong>${t.variant_name&&t.variant_count>1?`<em class="tc-variant">${escape(t.variant_name)}</em>`:''}<small>${escape(when)}</small></span><span class="tc-go" aria-hidden="true">↗</span></span>`
    +`<span class="tc-body"><span class="tc-metric"><small>成品</small><b>${done}<i>/${total}</i></b></span>${thumb}</span>`
    +`<span class="tc-foot"><span class="tc-dots">${dots}</span><span class="pill ${status[0]}">${status[1]}</span></span></button></div>`;
}
// 带操作按钮的提示（删除后撤销）
function toastAction(message,label,action,ms=10000){
  const box=$('toast');
  box.innerHTML=`<span>${escape(message)}</span><button type="button" class="toast-action">${escape(label)}</button>`;
  box.hidden=false;clearTimeout(toast.timer);
  box.querySelector('.toast-action').onclick=guard(async()=>{clearTimeout(toast.timer);box.hidden=true;await action();});
  toast.timer=setTimeout(()=>box.hidden=true,ms);
}
async function deleteTask(id,name){
  const wrap=$('task-list').querySelector(`[data-wrap="${CSS.escape(id)}"]`);
  wrap?.classList.add('removing');
  try{await api('/api/tasks/'+encodeURIComponent(id),undefined,'DELETE');}
  catch(error){wrap?.classList.remove('removing');throw error;}
  await new Promise(r=>setTimeout(r,260));
  await tasksList();
  toastAction(`已删除「${name}」，sku 里已入库的图不受影响`,'撤销',async()=>{
    await api('/api/tasks/'+encodeURIComponent(id)+'/restore',{});
    if(!$('start-page').hidden)await tasksList();
    toast(`已恢复「${name}」`);
  });
}
async function tasksList(){
  const version=++taskListVersion,product=scan;
  const current=()=>version===taskListVersion&&product===scan&&!$('start-page').hidden;
  const storage=api('/api/storage').then(data=>{if(current())renderStorage(data);},error=>{if(current()&&!error?.offline)toast(error.message);});
  const data=await api('/api/tasks'+(product?'?'+new URLSearchParams({product_dir:product.product_dir}):''));
  if(!current())return;
  $('task-list').innerHTML=data.tasks.length?data.tasks.map((t,i)=>taskCard(t,i)).join(''):'<div class="empty">还没有任务。选好产品文件夹后，点「新建任务」。</div>';
  $('task-list').querySelectorAll('.task-card').forEach(b=>{
    b.onclick=()=>location.hash='task/'+b.dataset.task;
    // 键盘：选中卡片后按 Delete 删除
    b.onkeydown=e=>{if(e.key==='Delete'){e.preventDefault();b.previousElementSibling?.click();}};
  });
  $('task-list').querySelectorAll('.tc-delete').forEach(b=>b.onclick=guard(()=>deleteTask(b.dataset.delete,b.dataset.name)));
  if(product&&data.tasks.length)$('start-refs').innerHTML=product.reference_images.map(p=>`<figure><img ${previewAttributes(fileUrl(p,data.tasks[0].id),base(p),'home-refs')} src="${fileUrl(p,data.tasks[0].id,360)}" alt="${escape(base(p))}" loading="lazy" decoding="async"><figcaption>${escape(base(p))}</figcaption></figure>`).join('');
  await storage;
}
$('storage-cleanup').onclick=guard(async()=>{
  const storage=await api('/api/storage');
  renderStorage(storage);
  if(!storage.cleanable_tasks||$('start-page').hidden)return;
  modal('清理已入库任务',`<div class="storage-confirm-summary"><strong>${storage.cleanable_tasks} 个任务</strong><strong>${storageSize(storage.cleanable_bytes)}</strong></div>
    <p>只删工作台里的中间文件，sku 里的成品不受影响。清理后，这些任务的小样和校准记录将无法查看。</p>
    <div class="dialog-actions"><button id="storage-cancel">取消</button><button id="storage-confirm" class="danger">确认清理</button></div>`);
  $('storage-cancel').onclick=()=>$('dialog').close();
  $('storage-cancel').focus();
  $('storage-confirm').onclick=guard(async()=>{
    const button=$('storage-confirm');
    button.disabled=true;
    try{
      const result=await api('/api/storage/cleanup',{mode:'delivered'});
      if(button.isConnected)$('dialog').close();
      if(!$('start-page').hidden)await tasksList();
      toast(`已清理 ${result.cleaned_tasks} 个任务 · ${storageSize(result.cleaned_bytes)}`);
    }finally{button.disabled=false;}
  });
});
async function scanProduct(path,selectedModel=''){
  $('scan-error').textContent='';const result=await api('/api/scan',{product_dir:path});
  if(scan?.product_dir!==result.product_dir)pendingReferences=[];
  scan=result;renderPendingReferences();
  localStorage.setItem('whitebg.product',scan.product_dir);$('product-path').value=scan.product_dir;
  $('scan-results').hidden=false;$('product-name').textContent=scan.product_name;
  $('models').innerHTML=scan.models.length?scan.models.map(p=>`<label class="model-choice"><input type="radio" name="model" value="${escape(p)}"><span>${escape(base(p))}<small>${escape(p.replace(scan.product_dir,''))}</small></span></label>`).join(''):'<p class="hint">产品文件夹里没有找到 .max 或 .fbx 文件。</p>';
  const samePath=(a,b)=>a.replaceAll('\\','/').toLowerCase()===b.replaceAll('\\','/').toLowerCase();
  const choices=[...$('models').querySelectorAll('input')];
  const choice=choices.find(i=>samePath(i.value,selectedModel))||choices.find(i=>i.value.toLowerCase().endsWith('.max'))||choices[0];
  function markSelection(){choices.forEach(input=>input.closest('label').classList.toggle('selected',input.checked));}
  if(choice)choice.checked=true;
  choices.forEach(input=>input.onchange=markSelection);markSelection();
  if(selectedModel&&!choices.some(i=>samePath(i.value,selectedModel)))toast('这个模型不在本次扫描范围内，请检查层级或所在文件夹。');
  $('start-refs').innerHTML=scan.reference_images.length?scan.reference_images.map(p=>`<div class="file-line">${escape(base(p))}</div>`).join(''):'<p class="hint">还没有参考图。新建任务后可查看文件夹中的参考图。</p>';
  $('sku-hint').textContent=scan.sku_exists?`已有 ${scan.sku_files.length} 个文件`:'入库时创建 sku 文件夹。';
  $('sku-files').innerHTML=scan.sku_files.map(p=>`<div class="file-line">${escape(p.replace(scan.product_dir+'\\sku\\','').replace(scan.product_dir+'/sku/',''))}</div>`).join('');
  await tasksList();
}
$('scan-form').onsubmit=async e=>{e.preventDefault();try{await scanProduct($('product-path').value.trim());}catch(error){$('scan-error').textContent=error.message;}};
async function browse(path=''){
  const data=await api('/api/folders?'+new URLSearchParams({path}));
  modal('选产品文件夹',`<div class="folder-location">${escape(data.path||'此电脑')}</div><div class="folder-list">${data.path?'<button data-parent>↑ 上一级</button>':''}${data.directories.map(d=>`<button data-path="${escape(d.path)}">▱　${escape(d.name)}</button>`).join('')}</div><div class="dialog-actions"><button class="primary" id="use-folder" ${data.path?'':'disabled'}>选用此文件夹</button></div>`);
  $('dialog-content').querySelector('[data-parent]')?.addEventListener('click',guard(()=>browse(data.parent||'')));
  $('dialog-content').querySelectorAll('[data-path]').forEach(b=>b.ondblclick=guard(()=>browse(b.dataset.path)));
  $('use-folder').onclick=guard(async()=>{$('dialog').close();await scanProduct(data.path);});
}
$('browse').onclick=guard(()=>browse($('product-path').value.trim()||''));
$('create-task').onclick=guard(async()=>{
  const model=document.querySelector('input[name=model]:checked')?.value||'';
  $('create-task').disabled=true;
  try{
    const result=await api('/api/tasks',{product_dir:scan.product_dir,model_file:model});
    try{await uploadReferences(result.task.id,pendingReferences);pendingReferences=[];renderPendingReferences();}
    finally{location.hash='task/'+result.task.id;}
  }finally{$('create-task').disabled=false;}
});

function renderPendingReferences(){
  $('pending-refs').textContent=pendingReferences.length?`新建任务时一起加入 ${pendingReferences.length} 张参考图：`+pendingReferences.map(r=>r.path?base(r.path):r.file.name).join('、'):'';
}
async function uploadReferences(id,references){
  let result;
  for(const ref of references){
    if(ref.path)result=await api('/api/tasks/'+id+'/references',{paths:[ref.path]});
    else{
      if(ref.file.size>50*1024*1024)throw new Error('单张参考图不能超过 50 MB。');
      result=await uploadBlob('/api/tasks/'+id+'/references/upload?'+new URLSearchParams({name:ref.file.name}),ref.file,{
        headers:{'Content-Type':ref.file.type||'application/octet-stream'},
        fallback:'参考图上传失败',
      });
    }
  }
  return result;
}
async function acceptReferences(references){
  if(!references.length)return;
  if(task){
    await flush();const id=task.id;referenceBusy=true;
    try{
      const result=await uploadReferences(id,references);
      if(task?.id===id){task.reference_images=result.task.reference_images;reference=task.reference_images.at(-1);renderReferences();savePreferences();}
      toast(`已加入 ${references.length} 张参考图。`);
    }finally{referenceBusy=false;}
  }else if(scan){pendingReferences.push(...references);renderPendingReferences();toast(`已暂存 ${references.length} 张参考图，新建任务时会一起加入。`);}
  else toast('请先选择并扫描产品文件夹，再添加参考图。');
}
function enqueueInput(fn){inputQueue=inputQueue.catch(()=>{}).then(fn).catch(error=>toast(error.message));return inputQueue;}
async function handleDesktopInput(plan){
  if(!plan)return;
  const wasTask=!!task;
  // 图片与模型同时拖入任务页时，图片仍归属于原来的当前任务。
  if(wasTask)await acceptReferences((plan.images||[]).map(path=>({path})));
  if(plan.product){
    await flush();history.replaceState(null,'',location.pathname+location.search);
    await route({skipScan:true});
    await scanProduct(plan.product.product_dir,plan.product.selected_model);
  }
  if(!wasTask)await acceptReferences((plan.images||[]).map(path=>({path})));
  const messages=[];
  if(plan.missing)messages.push('没有拿到这个文件的位置，请用「选择产品文件夹」按钮');
  if(plan.ignored)messages.push(`已忽略其余 ${plan.ignored} 项；一次只打开第一个文件夹或模型，图片会作为参考图加入。`);
  if(messages.length)toast(messages.join('；'));
}
function enableDesktop(){
  if(!window.pywebview?.api?.choose_folder)return;
  desktop=true;$('desktop-buttons').hidden=false;$('browse').hidden=true;
  $('drop-hint').textContent='拖入产品文件夹或模型';
}
window.addEventListener('pywebviewready',enableDesktop);
window.addEventListener('whitebgdesktopready',enableDesktop);
$('choose-product-folder').onclick=guard(async()=>enqueueInput(async()=>handleDesktopInput(await window.pywebview.api.choose_folder())));
$('choose-model-file').onclick=guard(async()=>enqueueInput(async()=>handleDesktopInput(await window.pywebview.api.choose_model())));
$('start-add-reference').onclick=()=>$('start-reference-input').click();
$('add-reference').onclick=()=>$('reference-input').click();
for(const id of ['start-reference-input','reference-input'])$(id).onchange=e=>{
  const files=[...e.target.files];e.target.value='';enqueueInput(()=>handleBrowserImages(files));
};
async function handleBrowserImages(files){
  const images=files.filter(f=>/\.(jpe?g|png|webp)$/i.test(f.name));
  await acceptReferences(images.map(file=>({file})));
  if(files.length!==images.length)toast('只接受 jpg、jpeg、png、webp 图片；拖入文件夹需要用桌面窗口打开（双击 Panda Studio）。');
}
let dragDepth=0,dragTimer;
const fileDrag=e=>[...(e.dataTransfer?.types||[])].includes('Files');
function hideDropOverlay(){dragDepth=0;clearTimeout(dragTimer);$('desktop-drop-overlay').hidden=true;}
document.addEventListener('dragenter',e=>{if(fileDrag(e)){e.preventDefault();dragDepth++;if(location.hash==='#refs')calibrationUI.rememberDropTarget(e.target);if(desktop&&!calibrationUI.calibrationOpen()&&location.hash!=='#canvas')$('desktop-drop-overlay').hidden=false;}},true);
document.addEventListener('dragover',e=>{if(fileDrag(e)){e.preventDefault();if(location.hash==='#refs')calibrationUI.rememberDropTarget(e.target);if(desktop&&!calibrationUI.calibrationOpen()&&location.hash!=='#canvas')$('desktop-drop-overlay').hidden=false;clearTimeout(dragTimer);dragTimer=setTimeout(hideDropOverlay,1500);}},true);
document.addEventListener('dragleave',e=>{if(fileDrag(e)&&(--dragDepth<=0||!e.relatedTarget))hideDropOverlay();},true);
document.addEventListener('drop',e=>{
  hideDropOverlay();
  if(calibrationUI.calibrationOpen()&&fileDrag(e)){
    e.preventDefault();
    const files=[...e.dataTransfer.files];
    if(!desktop)enqueueInput(()=>calibrationUI.dropTaskFiles(files));
    return;
  }
  if(location.hash==='#canvas'){
    if(!fileDrag(e))return;
    e.preventDefault();
    const files=[...e.dataTransfer.files],x=e.clientX,y=e.clientY;
    if(!desktop)enqueueInput(()=>canvasUI.dropFiles(files,x,y));
    return;
  }
  if(location.hash==='#refs'){
    if(!fileDrag(e)&&!e.dataTransfer?.getData('text/plain'))return;
    e.preventDefault();
    calibrationUI.rememberDropTarget(e.target);
    // 拖放事件结束后，浏览器可能清空路径文本，先同步取出内容。
    const text=e.dataTransfer.getData('text/plain'),input={target:e.target,dataTransfer:{files:[...e.dataTransfer.files],getData:()=>text}};
    if(!desktop||!fileDrag(e))enqueueInput(()=>calibrationUI.handleDrop(input));return;
  }
  if(!fileDrag(e))return;e.preventDefault();
  if(!desktop){
    const files=[...e.dataTransfer.files];
    if(!files.length)toast('没有拿到这个文件的位置，请用「浏览文件夹」按钮。');
    else enqueueInput(()=>handleBrowserImages(files));
  }
},true);
window.addEventListener('blur',hideDropOverlay);
window.whitebgDesktopDrop=plan=>{
  hideDropOverlay();
  if(calibrationUI.calibrationOpen())enqueueInput(()=>calibrationUI.dropTaskPaths(plan.images||[]));
  else if(location.hash==='#canvas')enqueueInput(()=>canvasUI.addPaths(plan.images||[]));
  else enqueueInput(()=>location.hash==='#refs'?calibrationUI.addDesktopPaths(plan.images||[]):handleDesktopInput(plan));
};

const aspectOf=it=>it.orientation==='landscape'?4/3:3/4;
const controls=[['azimuth','左右转动',-180,180,'°'],['elevation','俯视角度',-15,85,'°'],
  ['distance','产品大小',50,160,'%'],['vertical','上下位置',-50,50,'%'],
  ['horizontal','左右位置',-50,50,'%'],['focal','透视',24,135,' mm']];
for(const [key,label,min,max,unit] of controls){
  $(key==='focal'?'advanced-controls':'pose-controls').insertAdjacentHTML('beforeend',`<label class="control"><span>${label}<output id="value-${key}"></output></span><input type="range" id="control-${key}" min="${min}" max="${max}" step="1"></label>`);
}
function setText(element,value){if(element.textContent!==value)element.textContent=value;}
function framing(it,s){
  const box=viewer?.model?.box;
  const pose=it.camera;
  if(!box)return {center:pose.target_mm,radius:s.radius,width:1,height:1};
  const fit=fitPose({box,azimuth:s.azimuth,elevation:s.elevation,fovY:pose.fov_y_deg,aspect:aspectOf(it),verticalCorrection:pose.vertical_correction});
  return {center:fit.target_mm,radius:fit.distance_mm,width:Math.max(1,viewer.model.size[0]),height:Math.max(1,viewer.model.size[1])};
}
function syncControls(){
  const it=item(),p=it.camera,s=toSpherical(p),{right}=basis(s.azimuth,s.elevation);
  const correction=$('vertical-correction');
  correction.checked=p.vertical_correction===true;
  correction.disabled=!canCorrectVertically(s.elevation);
  $('vertical-correction-note').hidden=!correction.disabled;
  const {center,radius,width,height}=framing(it,s);
  const values={...s,distance:radius/s.radius*100,focal:focalLengthMm(p.fov_y_deg),
    vertical:(p.target_mm[1]-center[1])/height*100,
    horizontal:p.target_mm.reduce((sum,n,i)=>sum+(n-center[i])*right[i],0)/width*100};
  for(const [key,,,,unit] of controls){
    syncRange($('control-'+key),Math.round(values[key]));
    setText($('value-'+key),Math.round(values[key])+unit);
  }
}
for(const [key] of controls)$('control-'+key).oninput=()=>{
  const it=item();if(!it||!viewer?.model)return;
  const p=it.camera,s=toSpherical(p),value=Number($('control-'+key).value);
  let target=[...p.target_mm],fov=p.fov_y_deg;
  if(key==='azimuth')s.azimuth=value;
  if(key==='elevation')s.elevation=value;
  if(key==='focal')fov=fovFromFocal(value);
  if(key==='distance'||key==='vertical'||key==='horizontal'){
    const {center,radius,width,height}=framing(it,s);
    if(key==='distance')s.radius=radius*100/value;
    if(key==='vertical')target[1]=center[1]+value*height/100;
    if(key==='horizontal'){
      const {right}=basis(s.azimuth,s.elevation);
      const offset=target.reduce((sum,n,i)=>sum+(n-center[i])*right[i],0);
      target=target.map((n,i)=>n+right[i]*(value*width/100-offset));
    }
  }
  it.camera=fromSpherical(target,s.radius,s.azimuth,s.elevation,fov,p.vertical_correction);changed();
};
let staleRows=new Set(),saveDue=0,saveScheduled=false,beforeLighting=null;
function invalidate(it){
  for(const mode of renderModes){
    if(it[mode].state!=='none'||it[mode].job_id)staleRows.add(it);
    it[mode].state='none';it[mode].job_id='';
  }
}
function saveState(state){
  $('save-state').dataset.state=state;
  $('save-state').title=state==='saved'?'已保存':'';
  setText($('save-state'),state==='saving'?'保存中':state==='failed'?'保存失败':'');
}
function changed(invalidatePicture=true){
  if(invalidatePicture)invalidate(item());
  revision++;dirty=true;saveDue=performance.now()+400;saveScheduled=true;
  scheduleDraw();
}
function scheduleSave(){
  if(!saveScheduled)return;
  saveScheduled=false;saveState('saving');clearTimeout(saveTimer);
  saveTimer=setTimeout(()=>flush().catch(e=>toast(e.message)),Math.max(0,saveDue-performance.now()));
}
function waitWithAbort(promise,maxWaitMs,onTimeout){
  if(!(maxWaitMs>0))return promise;
  let timer;
  return Promise.race([
    promise.catch(()=>{}),
    new Promise(resolve=>{timer=setTimeout(()=>{onTimeout?.();resolve();},maxWaitMs);}),
  ]).finally(()=>clearTimeout(timer));
}
function syncSavedTask(saved,snapshotId,rev){
  if(!saved||task?.id!==snapshotId)return;
  if(task.items.length===saved.items.length){
    task.items.forEach((entry,i)=>{if(!entry.item_id&&saved.items[i]?.item_id)entry.item_id=saved.items[i].item_id;});
  }
  if(rev===revision){
    task=structuredClone(saved);
    lightingUI.notifyTaskChanged();
    selected=Math.max(0,Math.min(selected,task.items.length-1));
    saveConflict=false;saveState('saved');
    renderItems();renderSteps();
  }
}
async function flush({maxWaitMs=0}={}){
  clearTimeout(saveTimer);saveScheduled=false;
  if(!task||!dirty){
    await waitWithAbort(saving,maxWaitMs,()=>flushAbort?.abort());
    return;
  }
  const snapshot=structuredClone(task),rev=revision;dirty=false;saveState('saving');
  saving=saving.catch(()=>{}).then(async()=>{
    const controller=new AbortController();
    flushAbort=controller;
    try{
      const result=await api('/api/tasks/'+snapshot.id,{lighting:snapshot.lighting,items:snapshot.items,preview_rotation_deg:snapshot.preview_rotation_deg},'PUT',{signal:controller.signal});
      syncSavedTask(result?.task,snapshot.id,rev);
    }catch(error){
      if(task?.id===snapshot.id&&rev===revision){
        dirty=true;saveConflict=error.status===409;saveState('failed');
        if(saveConflict&&calibrationUI.busy()&&lastBundle?.progress){
          await undoRejectedCalibrationEdits(snapshot,rev).catch(()=>{});
        }
      }
      throw error;
    }finally{if(flushAbort===controller)flushAbort=null;}
  });
  await waitWithAbort(saving,maxWaitMs,()=>flushAbort?.abort());
}
async function undoRejectedCalibrationEdits(snapshot,rev){
  const data=await api('/api/tasks/'+snapshot.id);
  if(task?.id!==snapshot.id||rev!==revision||!['rendering','thinking'].includes(data.task.ai_calibration?.state))return;
  const previousSelection=item()?.item_id,edited=new Map(snapshot.items.map(entry=>[entry.item_id,entry]));
  updateBundle(data,false);
  task=structuredClone(data.task);beforeLighting=null;staleRows.clear();saveConflict=false;dirty=false;
  for(const entry of task.items){
    const local=edited.get(entry.item_id);if(!local)continue;
    for(const key of ['camera','orientation','parts']){
      if(JSON.stringify(entry[key])===JSON.stringify(local[key]))continue;
      entry[key]=local[key];dirty=true;
      if(key!=='parts')invalidate(entry);
    }
  }
  const index=task.items.findIndex(entry=>entry.item_id===previousSelection);
  selected=index>=0?index:Math.min(selected,task.items.length-1);
  renderItems();renderParts();renderSteps();scheduleDraw(true);
  if(dirty)changed(false);else saving=Promise.resolve();
}
const presets=[['正面',0,6],['右前',35,12],['左前',-35,12],['右侧',90,10],['左侧',-90,10],['俯视',35,35],['背面',180,6],['自动取景'],['重置']];
$('presets').innerHTML=presets.map(([label],i)=>`<button data-preset="${i}">${label}</button>`).join('');
function fit(a,e,reset=false){
  const it=item();
  if(!viewer?.model){if(reset&&it.camera.vertical_correction){it.camera.vertical_correction=false;changed();}return;}
  const s=toSpherical(it.camera);a??=s.azimuth;e??=s.elevation;
  if(reset)it.orientation=projectedOrientation(viewer.model.box,a,e);
  it.camera=fitPose({box:viewer.model.box,azimuth:a,elevation:e,fovY:reset?27:it.camera.fov_y_deg,aspect:aspectOf(it),
    verticalCorrection:!reset&&it.camera.vertical_correction===true});changed();
}
$('presets').onclick=e=>{const b=e.target.closest('button');if(!b)return;const i=Number(b.dataset.preset);if(i<7)fit(presets[i][1],presets[i][2]);else if(i===7)fit();else{
  const v=item().view,angles=v==='front'?[0,6]:v==='side'?[-35,10]:v==='top'?[35,35]:v==='back'?[180,6]:[35,12];fit(...angles,true);
}};
const formatButtons=[...document.querySelectorAll('[data-orientation]')];
formatButtons.forEach(b=>b.onclick=()=>{if(item().orientation===b.dataset.orientation)return;item().orientation=b.dataset.orientation;if(viewer?.model)fit();else changed();});
$('vertical-correction').onchange=e=>{
  item().camera.vertical_correction=e.target.checked;
  if(viewer?.model)fit();else changed();
};
function renderItems(){
  const key=JSON.stringify([task.id,task.items.map(i=>[i.no,i.label])]);
  if($('items').dataset.structure!==key){
    $('items').dataset.structure=key;
    $('items').innerHTML=task.items.map((it,i)=>`<div class="item-row" data-index="${i}"><span class="drag-handle" draggable="true" title="拖动排序">⋮⋮</span><input type="checkbox" aria-label="启用第 ${i+1} 张"><span class="item-no">${String(it.no).padStart(2,'0')}</span><div class="item-info"><strong>${escape(it.label)}</strong><span class="state"></span></div><div class="item-thumb"></div><button class="delete-item" title="删除这一张" aria-label="删除第 ${it.no} 张">×</button></div>`).join('');
    bindRows();
  }
  task.items.forEach(updateRow);updateCount();
}
function updateCount(){
  setText($('item-count'),`${task.items.filter(i=>i.enabled).length}/${task.items.length}`);
}
function resultUrl(result,it,w=0){
  const path=outputPath(result);
  if(!path)return '';
  const ver=[result.job_id,it?.item_id].filter(Boolean).join('-');
  return fileUrl(path,task?.id,w)+(ver?'&v='+encodeURIComponent(ver):'');
}
function visibleResult(it){
  const available=lastBundle?.available_outputs;
  for(const mode of ['final','smallshot']){
    if(it[mode].state==='done'&&(!available||available[mode].includes(it.no)))return it[mode];
  }
  return null;
}
function updateRow(it,index){
  const row=$('items').children[index];if(!row)return;
  row.classList.toggle('selected',index===selected);
  row.querySelector('input').checked=it.enabled;
  const {state,text,message}=rowStatus(it);
  row.classList.toggle('is-running',state==='queued'||state==='rendering');
  const badge=row.querySelector('.state');
  badge.className='state '+state;
  badge.title=state==='failed'?message||'':'';
  setText(badge,text);
  const thumb=row.querySelector('.item-thumb');
  const result=visibleResult(it),fullUrl=result?resultUrl(result,it):'',thumbUrl=result?resultUrl(result,it,160):'';
  const key=thumbUrl||(task.cleaned_at?'已清理':'');
  if(thumb.dataset.url!==key){
    thumb.dataset.url=key;
    thumb.innerHTML=thumbUrl?`<img ${previewAttributes(fullUrl,`${String(it.no).padStart(2,'0')} ${it.label} · ${result===it.final?'成品':'小样'}`,'task-items')} src="${escape(thumbUrl)}" alt="第 ${it.no} 张" decoding="async">`:key?`<span class="cleaned-thumb">${key}</span>`:'';
  }
}
async function commitItems(id,nextItems,nextSelected){
  await flush();
  if(task?.id!==id)return;
  const rev=++revision;saveState('saving');
  let saved;
  try{
    saved=await api('/api/tasks/'+id,{lighting:task.lighting,items:nextItems,preview_rotation_deg:task.preview_rotation_deg},'PUT');
  }catch(error){
    if(task?.id===id&&rev===revision)saveState('failed');
    throw error;
  }
  if(task?.id!==id)return;
  task=structuredClone(saved.task);
  lightingUI.notifyTaskChanged();
  selected=Math.max(0,Math.min(nextSelected,task.items.length-1));
  dirty=false;saveConflict=false;staleRows.clear();saveState('saved');
  bundleKey=appliedKey=null;
  if(lastBundle?.available_outputs)delete lastBundle.available_outputs;
  renderItems();renderSteps();renderParts();scheduleDraw(true);
  if(task.cleaned_at)refresh(true).catch(()=>{});
}
function bindRows(){
  $('items').querySelectorAll('.item-row').forEach(row=>{
    const index=Number(row.dataset.index);
    row.onclick=e=>{
      if(e.target.closest('input,button,[data-preview]')||index===selected)return;
      $('items').children[selected]?.classList.remove('selected');selected=index;row.classList.add('selected');
      renderParts();scheduleDraw(true);
    };
    row.querySelector('input').onchange=e=>{task.items[index].enabled=e.target.checked;updateCount();renderSteps();changed(false);};
    row.querySelector('.delete-item').onclick=guard(async()=>{
      await flush();
      if(task.items.length===1)return toast('清单至少保留一张。');
      if(hasRunning())return toast('请等当前任务结束后再删除。');
      if(calibrationUI.busy())return toast('AI 校准进行中：结束后再改打光或清单。');
      const id=task.id,removed=structuredClone(task.items[index]);
      const next=renumberItems(task.items.filter((_,i)=>i!==index));
      const nextSelected=selected>index?selected-1:Math.min(selected,next.length-1);
      await commitItems(id,next,nextSelected);
      toastAction(`已删除「${removed.label}」`,'撤销',async()=>{
        if(task?.id!==id)return;
        await flush();
        if(hasRunning())return toast('请等当前任务结束后再恢复。');
        if(calibrationUI.busy())return toast('AI 校准进行中：结束后再改打光或清单。');
        const insertAt=Math.min(index,task.items.length);
        const restoredList=[...task.items];
        restoredList.splice(insertAt,0,structuredClone(removed));
        await commitItems(id,renumberItems(restoredList),insertAt);
        toast(`已恢复「${removed.label}」`);
      });
    });
    row.querySelector('.drag-handle').ondragstart=e=>{if(calibrationUI.busy()){e.preventDefault();return;}dragNo=index;e.dataTransfer.setData('text/plain',String(index));};
    row.querySelector('.drag-handle').ondragend=()=>{dragNo=null;};
    row.ondragover=e=>{e.preventDefault();row.classList.add('drag-over');};row.ondragleave=()=>row.classList.remove('drag-over');
    row.ondrop=guard(async e=>{
      e.preventDefault();row.classList.remove('drag-over');
      const from=dragNo;dragNo=null;
      if(from===null||from===index)return;
      if(hasRunning())return toast('请等当前任务结束后再排序。');
      if(calibrationUI.busy())return toast('AI 校准进行中：结束后再改打光或清单。');
      const id=task.id;
      await flush();
      if(task?.id!==id||from>=task.items.length||index>=task.items.length)return;
      const current=item(),next=[...task.items];
      const [moving]=next.splice(from,1);
      next.splice(index,0,moving);
      const nextSelected=Math.max(0,next.indexOf(current));
      await commitItems(id,renumberItems(next),nextSelected);
    });
  });
}
function hasRunning(){return hasRunningItems(task);}
// ── 加一张：可搜索的视角菜单（参考 cmdk）。新任务只有「主图」，其余按需从这里加。
// 开合项会把对应类型的可动部件打开；输入列表里没有的名字时可直接新建。
const VIEW_CHOICES=[
  {group:'常用视角',label:'主图',view:'main',a:35,e:12,hint:'右前 35° · 略俯视'},
  {group:'常用视角',label:'正面',view:'front',a:0,e:6,hint:'正对 · 平视'},
  {group:'常用视角',label:'侧面',view:'side',a:-35,e:10,hint:'左前 35°'},
  {group:'常用视角',label:'左侧面',view:'custom',a:-90,e:8,hint:'纯侧 90°'},
  {group:'常用视角',label:'右侧面',view:'custom',a:90,e:8,hint:'纯侧 90°'},
  {group:'常用视角',label:'俯视',view:'top',a:35,e:35,hint:'右前 · 俯视 35°'},
  {group:'常用视角',label:'侧俯',view:'custom',a:-35,e:30,hint:'左前 · 俯视 30°'},
  {group:'常用视角',label:'背面',view:'back',a:180,e:6,hint:'隔断柜、岛台、双面柜'},
  {group:'开合状态',label:'抽屉打开',view:'custom',a:35,e:22,hint:'拉开抽屉 · 略俯视',open:['drawer','telescope']},
  {group:'开合状态',label:'柜门打开',view:'custom',a:35,e:12,hint:'打开柜门',open:['door','flap']},
];
const picker={open:false,active:0,options:[]};
function pickerOptions(query){
  const q=query.trim(),count=label=>task.items.filter(i=>i.label===label||i.label.startsWith(label+' ')).length;
  const list=VIEW_CHOICES.filter(c=>!q||c.label.includes(q)||c.hint.includes(q)).map(c=>({...c,count:count(c.label)}));
  if(q&&!VIEW_CHOICES.some(c=>c.label===q))list.push({group:'新建',label:q,view:'custom',a:35,e:12,hint:'用当前角度起步',custom:true,count:count(q)});
  return list;
}
function renderPicker(){
  const options=picker.options=pickerOptions($('view-search').value);
  picker.active=Math.min(picker.active,Math.max(0,options.length-1));
  let group='';
  $('view-options').innerHTML=options.length?options.map((c,i)=>{
    const head=c.group!==group?`<div class="popover-label">${escape(group=c.group)}</div>`:'';
    return head+`<div class="view-option${i===picker.active?' active':''}" role="option" id="view-opt-${i}" data-index="${i}" aria-selected="${i===picker.active}">`
      +`<span class="view-icon" aria-hidden="true">${c.custom?'＋':viewGlyph(c)}</span>`
      +`<span class="view-label"><strong>${c.custom?`新建「${escape(c.label)}」`:escape(c.label)}</strong><small>${escape(c.hint)}</small></span>`
      +(c.count?`<span class="view-count">已有 ${c.count}</span>`:'')+`</div>`;
  }).join(''):'<div class="view-empty">没有匹配的视角</div>';
  $('view-search').setAttribute('aria-activedescendant',options.length?`view-opt-${picker.active}`:'');
  $('view-options').querySelector('.active')?.scrollIntoView({block:'nearest'});
}
// 小图标：一个箭头指示从哪个方向看（俯视时箭头更陡）
function viewGlyph(c){
  const r=(c.a+180)*Math.PI/180,len=6+4*Math.cos(c.e*Math.PI/180),x=12+Math.sin(r)*len,y=12+Math.cos(r)*len*.62;
  return `<svg viewBox="0 0 24 24"><rect x="8" y="9" width="8" height="7" rx="1.4"/><path d="M${x.toFixed(1)} ${y.toFixed(1)}L12 12.5"/><circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="1.7"/></svg>`;
}
// 左侧栏是可滚动的毛玻璃（backdrop-filter 会裁掉里面的固定定位元素），菜单挂到页面最外层
document.body.append($('view-picker'));
function placePicker(){
  const r=$('add-item').getBoundingClientRect(),panel=$('view-picker'),h=Math.min(440,innerHeight*.7);
  const below=innerHeight-r.bottom-12>=Math.min(h,320);
  panel.style.left=Math.max(8,Math.min(r.left,innerWidth-308))+'px';
  panel.style.top=below?(r.bottom+8)+'px':'';
  panel.style.bottom=below?'':(innerHeight-r.top+8)+'px';
}
function openPicker(){
  if(!task)return;
  if(hasRunning())return toast('请等当前任务结束后再增加视角。');
  picker.open=true;$('view-search').value='';picker.active=firstUnused();
  $('view-picker').hidden=false;$('add-item').setAttribute('aria-expanded','true');placePicker();
  renderPicker();requestAnimationFrame(()=>$('view-search').focus());
}
addEventListener('resize',()=>{if(picker.open)placePicker();});
document.querySelector('.checklist').addEventListener('scroll',()=>{if(picker.open)placePicker();},{passive:true});
function closePicker(focus=true){
  if(!picker.open)return;
  picker.open=false;$('view-picker').hidden=true;$('add-item').setAttribute('aria-expanded','false');
  if(focus)$('add-item').focus();
}
function addView(choice){
  if(hasRunning())return toast('请等当前任务结束后再增加视角。');
  if(calibrationUI.busy())return toast('AI 校准进行中：结束后再改打光或清单。');
  const label=choice.count?`${choice.label} ${choice.count+1}`:choice.label;
  const no=task.items.length+1;
  const it=structuredClone(item());delete it.item_id;
  it.no=no;it.view=choice.view;it.label=label.slice(0,80);it.camera.vertical_correction=false;it.lighting=null;
  it.smallshot={state:'none',file:`smallshots/${no}.jpg`,job_id:''};
  it.final={state:'none',file:`final/${no}.jpg`,mask:`final/${no}_遮罩_ID.jpg`,job_id:''};
  const parts=viewer?.model?.parts||[];
  it.parts=Object.fromEntries(Object.keys(it.parts).map(k=>[k,choice.open&&choice.open.includes(parts.find(p=>p.name===k)?.type)?1:0]));
  task.items.push(it);selected=task.items.length-1;closePicker(false);
  if(choice.custom)fit(undefined,undefined,false);else fit(choice.a,choice.e,true);
  renderItems();renderSteps();renderParts();changed(false);scheduleDraw(true);
  requestAnimationFrame(()=>$('items').children[selected]?.scrollIntoView({block:'nearest',behavior:'smooth'}));
  toast(`已加「${label}」。`);
}
$('add-item').onclick=()=>picker.open?closePicker():openPicker();
// 默认选中第一个还没加过的视角（搜索时选第一个匹配项）
$('view-search').oninput=()=>{picker.active=0;renderPicker();};
const firstUnused=()=>Math.max(0,pickerOptions('').findIndex(c=>!c.count));
$('view-search').onkeydown=e=>{
  const n=picker.options.length;
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();if(n){picker.active=(picker.active+(e.key==='ArrowDown'?1:n-1))%n;renderPicker();}}
  else if(e.key==='Enter'){e.preventDefault();const c=picker.options[picker.active];if(c)addView(c);}
  else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();closePicker();}
};
$('view-options').onpointermove=e=>{const o=e.target.closest('.view-option');if(o&&Number(o.dataset.index)!==picker.active){picker.active=Number(o.dataset.index);renderPicker();}};
$('view-options').onclick=e=>{const o=e.target.closest('.view-option');if(o)addView(picker.options[Number(o.dataset.index)]);};
document.addEventListener('pointerdown',e=>{
  if(picker.open&&!e.target.closest('.add-item-wrap,#view-picker'))closePicker(false);
  if(switcher.open&&!e.target.closest('.variant-switcher'))closeSwitcher(false);
},true);

// ── 柜子切换器（参考 shadcn/ui 侧栏团队切换器）：一个场景里有多个柜子时，每个柜子是一个独立任务，
// 这里在同一模型的这些任务之间切换。Ctrl+1…9 直接切换。
const switcher={open:false,active:0,colors:new Map(),siblings:[]};
const swatchStyle=colors=>{
  const [a='#c9c3b8',b=a]=colors||[];
  return `background:conic-gradient(from 210deg,${a} 0 50%,${b} 50% 100%)`;
};
function variantList(){return task?.variants||[];}
function renderSwitcher(){
  const list=variantList(),current=task?.variant;
  const show=!!current&&list.length>1;
  $('variant-switcher').hidden=!show;
  if(!show){closeSwitcher(false);return;}
  const index=list.findIndex(v=>v.id===current.id);
  $('variant-swatch').setAttribute('style',swatchStyle(switcher.colors.get(current.id)));
  setText($('variant-name'),current.name);
  const mine=switcher.siblings.find(s=>s.id===task.id);
  setText($('variant-meta'),`柜子 ${index+1}/${list.length}`+(mine?` · 成品 ${mine.final_done||0}/${mine.item_count||0}`:''));
  if(switcher.open)renderSwitcherOptions();
}
function renderSwitcherOptions(){
  const list=variantList();
  $('variant-options').innerHTML=list.map((v,i)=>{
    const sib=switcher.siblings.find(s=>s.variant_id===v.id),isCurrent=v.id===task.variant.id;
    const progress=sib?`成品 ${sib.final_done||0}/${sib.item_count||0}`:'还没开始';
    return `<div class="variant-option${i===switcher.active?' active':''}${isCurrent?' current':''}" role="option" id="variant-opt-${i}" data-index="${i}" aria-selected="${isCurrent}">`
      +`<span class="variant-swatch" style="${swatchStyle(switcher.colors.get(v.id))}" aria-hidden="true"></span>`
      +`<span class="variant-text"><strong>${escape(v.name)}</strong><small>${escape(progress)}</small></span>`
      +(isCurrent?'<svg class="variant-check" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>':`<kbd>Ctrl ${i+1}</kbd>`)+`</div>`;
  }).join('');
}
async function loadSiblings(){
  if(!task?.variant_group)return;
  const data=await api('/api/tasks?'+new URLSearchParams({product_dir:task.product_dir}));
  switcher.siblings=data.tasks.filter(t=>t.variant_group===task.variant_group).map(t=>({...t,variant_id:t.variant_id||variantIdOf(t)}));
  renderSwitcher();
}
// 任务列表只给了款式名，按名字对回 id
function variantIdOf(t){return variantList().find(v=>v.name===t.variant_name)?.id;}
function openSwitcher(){
  switcher.open=true;switcher.active=Math.max(0,variantList().findIndex(v=>v.id===task.variant.id));
  $('variant-menu').hidden=false;$('variant-trigger').setAttribute('aria-expanded','true');
  renderSwitcherOptions();loadSiblings().catch(()=>{});$('variant-menu').focus?.();
}
function closeSwitcher(focus=true){
  if(!switcher.open)return;
  switcher.open=false;$('variant-menu').hidden=true;$('variant-trigger').setAttribute('aria-expanded','false');
  if(focus)$('variant-trigger').focus();
}
async function switchVariant(variantId){
  closeSwitcher(false);
  if(!task||variantId===task.variant?.id)return;
  if(hasRunning())return toast('当前柜子还在出图，等它结束再切换。');
  await flush();
  const result=await api(`/api/tasks/${task.id}/switch-variant`,{variant_id:variantId});
  location.hash='task/'+result.task_id;
}
$('variant-trigger').onclick=()=>switcher.open?closeSwitcher():openSwitcher();
$('variant-trigger').onkeydown=e=>{if(e.key==='ArrowDown'&&!switcher.open){e.preventDefault();openSwitcher();}};
$('variant-options').onpointermove=e=>{const o=e.target.closest('.variant-option');if(o){switcher.active=Number(o.dataset.index);$('variant-options').querySelectorAll('.variant-option').forEach((x,i)=>x.classList.toggle('active',i===switcher.active));}};
$('variant-options').onclick=guard(async e=>{const o=e.target.closest('.variant-option');if(o)await switchVariant(variantList()[Number(o.dataset.index)].id);});
document.addEventListener('keydown',guard(async e=>{
  if(switcher.open){
    const n=variantList().length;
    if(e.key==='Escape'){e.preventDefault();closeSwitcher();return;}
    if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();switcher.active=(switcher.active+(e.key==='ArrowDown'?1:n-1))%n;renderSwitcherOptions();return;}
    if(e.key==='Enter'){e.preventDefault();await switchVariant(variantList()[switcher.active].id);return;}
  }
  // Ctrl+数字：直接切换柜子（只在任务页、焦点不在输入框时）
  if((e.ctrlKey||e.metaKey)&&/^[1-9]$/.test(e.key)&&task&&!$('variant-switcher').hidden&&!e.target.closest('input,textarea,[contenteditable]')){
    const v=variantList()[Number(e.key)-1];if(v){e.preventDefault();await switchVariant(v.id);}
  }
}));
$('variant-rename').onclick=()=>{
  closeSwitcher(false);
  modal('给柜子改名',`<p class="hint">入库时用作文件名前缀，例如「${escape(task.variant.name)}_1.jpg」。</p><input id="variant-name-input" type="text" maxlength="40" value="${escape(task.variant.name)}"><div class="dialog-actions"><button id="variant-name-cancel">取消</button><button class="primary" id="variant-name-save">保存</button></div>`);
  const input=$('variant-name-input');input.select();
  const save=guard(async()=>{
    const name=input.value.replace(/[<>:"/\\|?*]/g,'').trim();
    if(!name)return toast('名字不能为空。');
    const result=await api(`/api/tasks/${task.id}/variant-name`,{variant_id:task.variant.id,name});
    task.variants=result.task.variants;task.variant=result.task.variant;$('dialog').close();renderSwitcher();toast('已改名。');
  });
  $('variant-name-save').onclick=save;$('variant-name-cancel').onclick=()=>$('dialog').close();
  input.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();save();}};
};
$('variant-rescan').onclick=guard(async()=>{
  closeSwitcher(false);
  if(!task?.preview_fbx||lastBundle?.preview?.state!=='done'||loading)return toast('预览模型还没读完。');
  const id=task.id,response=await fetchWithTimeout(fileUrl(task.preview_fbx),{},{timeout:60000});
  if(!response.ok)throw new Error('预览模型读取失败。');
  let buffer=await response.arrayBuffer();
  if(task?.id!==id){buffer=null;return;}
  viewer??=new Viewer($('canvas'));
  const prepared=viewer.prepareFbx(buffer,{taskId:id});
  buffer=null;
  try{await registerVariants(task,prepared.asset,true);}finally{prepared.abort();}
  if(task?.id!==id)return;
  modelKey='';await refresh(true);toast('已重新识别柜子。');
});
// 识别柜子并登记到服务端；source 可为已解析的 Three 根节点或 ArrayBuffer
async function registerVariants(t,source,force=false){
  const found=detectVariants(source);
  if(!found.length)return;
  const matched=matchVariants(found,t.variants||[]);
  matched.forEach(v=>switcher.colors.set(v.id,v.colors));
  if(!force&&Array.isArray(t.variants))return;          // 已登记过：只更新色块
  const active=(t.variant&&matched.find(v=>v.id===t.variant.id))?t.variant.id:matched[0].id;
  const result=await api(`/api/tasks/${t.id}/variants`,{variants:matched.map(variantPayload),active});
  if(task?.id===t.id){task.variants=result.task.variants;task.variant=result.task.variant;task.variant_group=result.task.variant_group;}
  if(result.orphans?.length)toast(`有 ${result.orphans.length} 个旧柜子在重新识别后对不上，请检查。`);
}
let zoneSize={width:0,height:0},frameSize={width:0,height:0};
function resizeFrame(){
  if(!zoneSize.width||!zoneSize.height)return;
  const aspect=aspectOf(item());
  const step=item().orientation==='landscape'?4:3;
  const w=Math.max(0,Math.floor(Math.min(zoneSize.width-8,(zoneSize.height-8)*aspect)/step)*step);
  const h=w/aspect;
  if(w===frameSize.width&&h===frameSize.height)return;
  frameSize={width:w,height:h};$('frame').style.width=w+'px';$('frame').style.height=h+'px';
}
new ResizeObserver(([entry])=>{zoneSize=entry.contentRect;scheduleDraw();}).observe($('frame-zone'));
function scheduleDraw(lighting=false){
  updateLighting ||= lighting;
  if(!frameRequest)frameRequest=requestAnimationFrame(drawView);
}
function drawView(){
  frameRequest=0;
  const it=item();if(!it)return;
  scheduleSave();
  if(beforeLighting){
    for(const [entry,light] of beforeLighting){
      if(task.items.includes(entry)&&JSON.stringify(effectiveLighting(task,entry))!==light)invalidate(entry);
    }
    beforeLighting=null;
  }
  if(staleRows.size){
    for(const changedItem of staleRows){const index=task.items.indexOf(changedItem);if(index>=0)updateRow(changedItem,index);}
    staleRows.clear();renderSteps();
  }
  setText($('selected-label'),`${String(it.no).padStart(2,'0')} ${it.label}`);
  $('selected-label').title=describePose(it.camera);
  const presetName=currentPreset(),sizes=lastBundle?.output_presets?.[presetName]?.[it.orientation];
  setText($('output-size'),`${it.orientation==='landscape'?'横版':'竖版'} · ${sizes?sizes.join('×'):(it.orientation==='landscape'?'4000×3000':'3000×4000')}`);
  syncPresetButtons(presetName);
  if(!$('pose-panel').hidden){
    syncControls();formatButtons.forEach(button=>button.classList.toggle('active',button.dataset.orientation===it.orientation));
    syncParts();
  }
  resizeFrame();
  viewer?.update(it);
  if(updateLighting){lightingUI.sync();updateLighting=false;}
  viewer?.render(frameSize.width,frameSize.height);
}
function renderParts(){
  const parts=viewer?.model?.parts||[];$('parts-panel').hidden=!parts.length;
  if($('parts-controls').parts===parts)return;
  $('parts-controls').parts=parts;
  // 按部件名自然排序显示（抽屉1、抽屉2…），data-part 仍指向原下标
  const order=parts.map((p,i)=>i).sort((a,b)=>String(parts[a].name).localeCompare(String(parts[b].name),'zh-CN',{numeric:true}));
  $('parts-count').textContent=parts.length+' 个部件';
  $('parts-controls').innerHTML=order.map(i=>`<label class="control"><span>${escape(parts[i].name)}<output id="part-val-${i}"></output></span><input data-part="${i}" type="range" min="0" max="100"></label>`).join('');
  $('parts-controls').querySelectorAll('input').forEach(input=>input.oninput=()=>{const p=parts[Number(input.dataset.part)];item().parts[p.name]=Number(input.value)/100;changed(false);});
}
function syncParts(){
  const parts=viewer?.model?.parts;
  if(!parts?.length)return;
  const box=$('parts-controls');
  parts.forEach((part,i)=>{
    const value=Math.round((item().parts[part.name]||0)*100);
    const input=box.querySelector(`input[data-part="${i}"]`);if(!input)return;
    syncRange(input,value);setText($('part-val-'+i),value+'%');
  });
}
$('close-parts').onclick=()=>{for(const k of Object.keys(item().parts))item().parts[k]=0;changed(false);};
const tabs=[...document.querySelectorAll('[data-tab]')];
let currentTab='pose';
function activateTab(name){
  currentTab=tabs.some(b=>b.dataset.tab===name)?name:'pose';
  for(const button of tabs){
    const active=button.dataset.tab===currentTab;
    button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;
    $(button.getAttribute('aria-controls')).hidden=!active;
  }
  scheduleDraw(true);
}
tabs.forEach((button,index)=>{
  button.onclick=()=>{activateTab(button.dataset.tab);savePreferences();};
  button.onkeydown=e=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
    e.preventDefault();
    const next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(index+tabs.length+(e.key==='ArrowRight'?1:-1))%tabs.length;
    tabs[next].click();tabs[next].focus();
  };
});
function taskPrefsKey(){return 'whitebg.task-view.'+task.id;}
function savePreferences(){
  localStorage.setItem(taskPrefsKey(),JSON.stringify({tab:currentTab,reference,overlay:$('overlay-on').checked,
    side:$('side-on').checked,opacity:Number($('opacity').value)}));
}
function restorePreferences(){
  let prefs={};
  try{prefs=JSON.parse(localStorage.getItem(taskPrefsKey()))||{};}catch{}
  reference=prefs.reference||'';
  $('overlay-on').checked=!!prefs.overlay;$('side-on').checked=!!prefs.side;
  syncRange($('opacity'),prefs.opacity??35);
  activateTab(prefs.tab);
}
function renderReferences(){
  if(!task.reference_images.includes(reference))reference=task.reference_images[0]||'';
  $('reference-list').innerHTML=task.reference_images.length?task.reference_images.map((p,i)=>`<div class="reference-choice"><button data-ref="${i}" title="${escape(base(p))}"><img src="${fileUrl(p,task?.id,200)}" alt="参考图 ${i+1}" loading="lazy" decoding="async"></button><button class="reference-zoom" ${previewAttributes(fileUrl(p),base(p),'task-refs')}>放大</button></div>`).join(''):'<p class="hint">当前产品没有参考图。</p>';
  $('reference-list').querySelectorAll('[data-ref]').forEach(b=>b.onclick=()=>{reference=task.reference_images[Number(b.dataset.ref)];applyReference();savePreferences();});applyReference();
}
function applyReference(){
  const path=reference,side=$('side-reference').querySelector('img');
  $('overlay').hidden=!path||!$('overlay-on').checked;$('side-reference').hidden=!path||!$('side-on').checked;
  $('reference-list').querySelectorAll('[data-ref]').forEach(b=>b.classList.toggle('selected',task.reference_images[Number(b.dataset.ref)]===path));
  if(path){
    const url=fileUrl(path);
    if($('overlay').getAttribute('src')!==url)$('overlay').src=url;
    if(side.getAttribute('src')!==url)side.src=url;
    side.dataset.preview=url;side.dataset.caption=base(path);side.tabIndex=0;side.setAttribute('aria-label','放大：'+base(path));
  }else{delete side.dataset.preview;side.removeAttribute('src');$('overlay').removeAttribute('src');}
  syncOpacity();
}
function syncOpacity(){
  syncRange($('opacity'));$('overlay').style.opacity=Number($('opacity').value)/100;
  setText($('opacity-value'),$('opacity').value+'%');
}
$('overlay-on').onchange=$('side-on').onchange=()=>{applyReference();savePreferences();};
$('opacity').oninput=()=>{syncOpacity();savePreferences();};
document.addEventListener('click',e=>{
  document.querySelectorAll('.canvas-tools details[open]').forEach(details=>{if(!details.contains(e.target))details.open=false;});
  if(!$('canvas-help').parentElement.contains(e.target))showHelp(false);
});
function showHelp(open){
  const button=$('canvas-help');button.parentElement.classList.toggle('open',open);
  button.setAttribute('aria-expanded',String(open));
}
$('canvas-help').onclick=()=>showHelp($('canvas-help').getAttribute('aria-expanded')!=='true');
$('canvas-help').onkeydown=e=>{if(e.key==='Escape')showHelp(false);};

let pointer=null;
$('canvas').oncontextmenu=e=>e.preventDefault();
$('canvas').onpointerdown=e=>{
  if(!viewer?.model||pointer||![0,2].includes(e.button))return;
  const pose=item().camera,s=toSpherical(pose);
  const correction=verticalProjection(pose,s);
  pointer={id:e.pointerId,x:e.clientX,y:e.clientY,pan:e.button===2||e.shiftKey,pose,s,
    axes:basis(s.azimuth,correction?0:s.elevation),scale:(correction?.depth??s.radius)*Math.tan(pose.fov_y_deg*RAD/2)*2/frameSize.height};
  $('canvas').setPointerCapture(e.pointerId);
};
$('canvas').onpointermove=e=>{
  if(pointer?.id!==e.pointerId)return;
  const dx=e.clientX-pointer.x,dy=e.clientY-pointer.y,p=pointer.pose,s={...pointer.s},target=[...p.target_mm];
  if(pointer.pan){const {right,up}=pointer.axes,scale=pointer.scale;
    for(let i=0;i<3;i++)target[i]+=(-dx*right[i]+dy*up[i])*scale;
  }else{s.azimuth=((s.azimuth-dx*.3+180)%360+360)%360-180;s.elevation=clamp(s.elevation+dy*.25,-15,85);}
  item().camera=fromSpherical(target,s.radius,s.azimuth,s.elevation,p.fov_y_deg,p.vertical_correction);changed();
};
$('canvas').onpointerup=$('canvas').onpointercancel=$('canvas').onlostpointercapture=e=>{if(pointer?.id===e.pointerId)pointer=null;};
$('canvas').addEventListener('wheel',e=>{e.preventDefault();if(!viewer?.model)return;const p=item().camera,s=toSpherical(p);item().camera=fromSpherical(p.target_mm,clamp(s.radius*Math.exp(e.deltaY*.001),10,1e7),s.azimuth,s.elevation,p.fov_y_deg,p.vertical_correction);changed();},{passive:false});
$('canvas').ondblclick=()=>fit();

async function loadPreview(bundle){
  const t=task,key=t.id+'|'+t.preview_fbx+'|'+t.preview_rotation_deg,epoch=routeVersion;
  if(key===modelKey||loading===key)return;
  if(bundle.preview.state!=='done'){
    const failed=bundle.preview.state==='failed'||!t.model_max;
    $('loading-model').hidden=false;$('loading-model').classList.toggle('is-busy',!failed);
    $('loading-model').textContent=failed?'预览准备失败，可改用模型文件夹里的 FBX':'正在准备预览模型';
    $('model-message').hidden=!failed;
    $('model-message').textContent=!t.model_max?'没有可导出预览的 .max，请改用模型文件夹里的 FBX。':failed?bundle.preview.message||'预览准备失败。':'';
    return;
  }
  loading=key;
  try {
    $('loading-model').hidden=false;$('loading-model').classList.add('is-busy');$('loading-model').textContent='正在读取预览模型…';
    const response=await fetchWithTimeout(fileUrl(t.preview_fbx),{},{timeout:60000});if(!response.ok)throw new Error('预览模型读取失败。');
    let buffer=await response.arrayBuffer();
    if(task?.id!==t.id||epoch!==routeVersion){buffer=null;return;}
    viewer??=new Viewer($('canvas'));
    const prepared=viewer.prepareFbx(buffer,{taskId:t.id});
    buffer=null;
    let model;
    try{
      // 识别场景里有几个柜子：复用已解析好的模型对象，不再重复解析整份 ArrayBuffer。
      try{await registerVariants(task,prepared.asset);}catch(error){console.warn('识别柜子失败',error);}
      if(task?.id!==t.id||epoch!==routeVersion)return;
      renderSwitcher();loadSiblings().catch(()=>{});
      const keepNames=task.variant&&(task.variants||[]).length>1?new Set(task.variant.nodes):null;
      model=viewer.load(prepared,{bbox:bundle.preview.bbox,rotation:t.preview_rotation_deg,taskId:t.id,keepNames});
    }finally{
      prepared.abort();
    }
    modelKey=key;renderParts();
    $('loading-model').hidden=true;$('model-dimensions').textContent=model.size.map(v=>Math.round(v)).join(' × ')+' 毫米';
    $('model-message').hidden=!model.warnings.length;$('model-message').textContent=model.warnings.join(' ');
    $('rotate-model').hidden=t.preview_source!=='file';
    const rev=revision,result=await api('/api/tasks/'+t.id+'/draft',{size_mm:model.size,parts:model.parts.map(p=>({name:p.name,type:p.type})),initial:true});
    if(task?.id===t.id&&epoch===routeVersion){
      if(rev===revision&&!dirty){task=result.task;lightingUI.notifyTaskChanged();}
      renderItems();renderSteps();renderParts();scheduleDraw(true);
    }
  }catch(error){
    if(task?.id!==t.id||epoch!==routeVersion)return;
    $('loading-model').classList.remove('is-busy');$('loading-model').textContent=error.message;
    $('model-message').hidden=false;$('model-message').textContent=error.message;throw error;
  } finally {if(loading===key)loading=null;}
}
$('choose-fbx').onclick=guard(async()=>{
  $('preview-menu').open=false;
  await flush();const data=await api('/api/scan',{product_dir:task.product_dir}),files=data.models.filter(p=>p.toLowerCase().endsWith('.fbx'));
  modal('选择网页预览用的 FBX',files.length?files.map((p,i)=>`<button class="task-card" data-fbx="${i}">${escape(base(p))}</button>`).join(''):'<p class="hint">模型文件夹里还没有 FBX 文件。</p>');
  $('dialog-content').querySelectorAll('[data-fbx]').forEach(b=>b.onclick=guard(async()=>{await api('/api/tasks/'+task.id+'/preview',{source:'file',path:files[Number(b.dataset.fbx)]});$('dialog').close();await refresh(true);}));
});
$('rotate-model').onclick=guard(async()=>{
  $('preview-menu').open=false;
  task.preview_rotation_deg=(task.preview_rotation_deg+90)%360;changed();
  await flush();await refresh(true);fit();
});
// 正式图输出档位 4K / 6K / 8K（4K=3000×4000，6K=4500×6000，8K=6000×8000）
function currentPreset(){return task?.output_preset||lastBundle?.output_preset||'4K';}
function syncPresetButtons(name){
  document.querySelectorAll('[data-output-preset]').forEach(button=>{
    const on=button.dataset.outputPreset===name;
    button.classList.toggle('active',on);button.setAttribute('aria-checked',String(on));
  });
}
document.querySelectorAll('[data-output-preset]').forEach(button=>button.onclick=guard(async()=>{
  if(!task)return;
  const name=button.dataset.outputPreset,id=task.id;
  if(name===currentPreset())return;
  if(hasRunning())return toast('请等当前渲染结束后再改输出尺寸。');
  await flush();
  const hadFinals=task.items.some(i=>i.final.state==='done');
  await api('/api/tasks/'+id,{output_preset:name},'PUT');
  if(task?.id!==id)return;
  task.output_preset=name;syncPresetButtons(name);
  await refresh(true);scheduleDraw(true);
  toast(hadFinals?`已切换到 ${name}，成品需要重新渲染`:`输出尺寸：${name}`);
}));
function renderSteps(){
  for(const {mode,text,current,active} of taskSteps(task,lastBundle)){
    const button=$(mode);
    setText(button,text);button.classList.toggle('primary',current);button.classList.toggle('is-busy',active);
    if(current)button.setAttribute('aria-current','step');else button.removeAttribute('aria-current');
  }
}
function pollLater(){
  clearTimeout(pollTimer);
  if(!task||document.hidden)return;
  const delay=pollFailures?backoffDelay(pollFailures):pollDelay(lastBundle,calibrationUI.busy());
  pollTimer=setTimeout(()=>refresh().catch(e=>{if(!e?.offline&&!e?.aborted)toast(e.message);}),delay);
}
let failedJobs=new Set(),saveConflict=false;
function updateBundle(data,acceptTask){
  const key=bundleSignature(data);
  // 心跳可能独立于任务签名更新，状态片自行比较可见字段。
  runnerUI.update(data.runner);
  const runnerOnline=data.runner?.online;
  const updated=key!==bundleKey;
  if(updated){
    bundleKey=key;lastBundle=data;
    for(const job of data.jobs||[]){
      if(job.state==='failed'&&!failedJobs.has(job.job_id)){
        failedJobs.add(job.job_id);toast(job.message||'任务失败');
      }
    }
  }else if(lastBundle?.runner?.online!==runnerOnline)lastBundle={...lastBundle,runner:data.runner};
  if(!acceptTask||key===appliedKey){
    if(updated){calibrationUI.render();materialsUI.render();renderSteps();}
    return false;
  }
  appliedKey=key;
  const oldReferences=JSON.stringify(task.reference_images);
  const viewState=()=>JSON.stringify([item()?.label,item()?.camera,item()?.parts,item()?.orientation,effectiveLighting(task,item())]);
  const oldView=viewState();
  task=structuredClone(data.task);selected=Math.min(selected,task.items.length-1);
  lightingUI.notifyTaskChanged();
  renderItems();renderSteps();
  if(updated){calibrationUI.render();materialsUI.render();}
  if(JSON.stringify(task.reference_images)!==oldReferences)renderReferences();
  if(oldView!==viewState())scheduleDraw(true);
  return true;
}
async function refresh(afterMutation=false){
  if(!task)return;
  if(polling){
    if(!afterMutation)return polling;
    await polling.catch(()=>{});
    return refresh();
  }
  const id=task.id,epoch=routeVersion,controller=new AbortController();
  pollAbort=controller;
  polling=(async()=>{
    await saving.catch(()=>{});
    if(task?.id!==id||epoch!==routeVersion)return;
    try{
      const rev=revision,data=await api('/api/tasks/'+id,undefined,'GET',{signal:controller.signal});
      pollFailures=0;
      if(task?.id!==id||epoch!==routeVersion)return;
      if(updateBundle(data,rev===revision&&!dirty&&!referenceBusy&&!pointer&&dragNo===null)){
        loadPreview(data).catch(e=>{if(epoch===routeVersion&&!e?.offline)toast(e.message);});
      }
      if(saveConflict&&!calibrationUI.busy()&&dirty){saveConflict=false;await flush();}
    }catch(error){
      if(error?.offline)pollFailures++;
      throw error;
    }
  })();
  try{await polling;}finally{
    if(pollAbort===controller)pollAbort=null;
    polling=null;
    if(epoch===routeVersion&&task?.id===id)pollLater();
  }
}
let submitting=new Set();
async function submit(mode){
  if(submitting.has(mode))return;
  const id=task.id;
  submitting.add(mode);
  try{
    await flush();await lightingUI.images();await flush();
    if(task?.id!==id)return;
    // 旧服务默认把小样交给 AI，手动流程在该服务上先关闭旧开关。
    if(mode==='smallshot'&&!('progress' in lastBundle)&&task.ai_calibration?.enabled&&!calibrationUI.busy()){
      await api('/api/tasks/'+id,{ai_calibration:{enabled:false}},'PUT');
    }
    await api('/api/tasks/'+id+'/jobs',{mode});
    await refresh(true);
    if(task?.id!==id)return;
    if(!runnerUI.queued())toast(mode==='smallshot'?'已提交小样':'已提交渲染');
  }finally{submitting.delete(mode);}
}
$('smallshot').onclick=guard(()=>submit('smallshot'));$('final').onclick=guard(()=>submit('final'));
async function overview(){
  await refresh();modal('白底图总览',`<div class="overview-grid">${task.items.map(it=>{
    const result=visibleResult(it),fullUrl=result?resultUrl(result,it):'',thumbUrl=result?resultUrl(result,it,400):'';
    return `<button class="overview-card" ${fullUrl?previewAttributes(fullUrl,`${String(it.no).padStart(2,'0')} ${it.label} · ${result===it.final?'成品':'小样'}`,'overview'):''} ${fullUrl?'':'disabled'}>${thumbUrl?`<img src="${escape(thumbUrl)}" alt="第 ${it.no} 张" loading="lazy" decoding="async">`:`<div class="empty">${task.cleaned_at?'已清理':'尚未出图'}</div>`}<strong>${String(it.no).padStart(2,'0')}　${escape(it.label)}</strong><span>${rowStatus(it).text} · ${it.orientation==='landscape'?'横版':'竖版'}${it.enabled?'':' · 未启用'}</span></button>`;
  }).join('')}</div>`);

}
$('overview').onclick=guard(overview);
$('delivery').onclick=guard(async()=>{
  await flush();await lightingUI.images();await flush();const plan=await api('/api/tasks/'+task.id+'/delivery-plan',{});
  modal('确认入库前，请核对文件',`<p class="hint">目标：${escape(plan.sku_dir)}<br>同名旧文件先移入「旧版／时间」文件夹，再放入新图。不会删除旧文件。</p><table class="delivery-table"><thead><tr><th>文件名</th><th>本次操作</th></tr></thead><tbody>${plan.files.map(f=>`<tr><td>${escape(f.name)}</td><td class="${f.action==='new'?'new':'archive'}">${f.action==='new'?'新增':'旧文件移进旧版，再写入新文件'}</td></tr>`).join('')}</tbody></table><div class="dialog-actions"><button id="cancel-delivery">再检查一下</button><button class="primary" id="confirm-delivery">确认写入 sku</button></div>`);
  $('cancel-delivery').onclick=()=>$('dialog').close();$('confirm-delivery').onclick=guard(async()=>{
    $('confirm-delivery').disabled=true;
    try{const result=await api('/api/tasks/'+task.id+'/deliver',{token:plan.token});
      task.delivered=result.delivered;renderSteps();
      modal('入库完成',`<div class="delivery-complete">已写入 ${result.delivered.files.length} 个文件。<br>${escape(result.delivered.sku_dir)}${result.delivered.archive_dir?'<br>旧文件已归档：'+escape(result.delivered.archive_dir):''}</div>`);
      await refresh(true);
    }catch(error){$('confirm-delivery').disabled=false;throw error;}
  });
});
$('go-home').onclick=guard(async()=>{await flush({maxWaitMs:1500}).catch(()=>{});location.hash='';});
// 打开软件时回到上次用的页面：只记首页、画布、参考库、设置（任务可能已经删了，回首页从列表里点）
const LAST_PAGES=['#canvas','#refs','#settings'];
let currentPage='home';
function setCurrentPage(page){
  currentPage=page;
  syncBackgroundPause(document,currentPage);
  runnerUI?.setHomeActive?.(page==='home');
}
function rememberPage(){
  const page=LAST_PAGES.includes(location.hash)?location.hash:'';
  try{localStorage.setItem('whitebg.page',page);}catch{}
}
async function route({skipScan=false}={}){
  const epoch=++routeVersion;
  rememberPage();
  clearTimeout(pollTimer);
  pollAbort?.abort();pollAbort=null;polling=null;pollFailures=0;
  if($('dialog').open)$('dialog').close();
  if($('annotate-delete-dialog').open)$('annotate-delete-dialog').close();
  if($('calibration-ref-delete').open)$('calibration-ref-delete').close();
  if($('annotate-dialog').open)$('annotate-dialog').close();
  await flush({maxWaitMs:1500}).catch(()=>{});
  if(epoch!==routeVersion)return;
  task=null;pointer=null;dragNo=null;beforeLighting=null;staleRows.clear();viewer?.clear();
  lightingUI?.notifyTaskChanged?.();
  // 画布页：整页无限画布，生图用
  $('canvas-page').hidden=location.hash!=='#canvas';
  if(location.hash==='#canvas'){
    $('start-page').hidden=true;$('task-page').hidden=true;$('settings-page').hidden=true;$('refs-page').hidden=true;
    setCurrentPage('canvas');
    await canvasUI.show();return;
  }
  canvasUI.hide();
  if(location.hash==='#refs'){
    calibrationUI.rememberDropTarget(null);
    $('start-page').hidden=true;$('task-page').hidden=true;$('settings-page').hidden=true;$('refs-page').hidden=false;
    setCurrentPage('refs');
    await calibrationUI.showRefs();return;
  }
  if(location.hash==='#settings'){
    $('start-page').hidden=true;$('task-page').hidden=true;$('refs-page').hidden=true;$('settings-page').hidden=false;
    setCurrentPage('settings');
    await settingsUI.show();return;
  }
  $('refs-page').hidden=true;$('settings-page').hidden=true;
  const id=location.hash.match(/^#task\/(.+)$/)?.[1];
  $('start-page').hidden=!!id;$('task-page').hidden=!id;
  if(!id){
    setCurrentPage('home');settingsUI.checkReady();
    if(!skipScan){if(scan)await scanProduct(scan.product_dir);else await tasksList();}
    return;
  }
  setCurrentPage('task');
  runnerUI.update(null);
  const data=await api('/api/tasks/'+encodeURIComponent(id));
  if(epoch!==routeVersion)return;
  bundleKey=appliedKey=null;lastBundle=null;task=structuredClone(data.task);selected=0;modelKey='';failedJobs.clear();
  lightingUI.notifyTaskChanged();
  $('task-product').textContent=task.product_name;$('task-product').title='任务 '+task.id;
  $('model-dimensions').textContent='';$('rotate-model').hidden=true;$('preview-menu').open=false;showHelp(false);
  $('advanced').open=false;$('model-message').hidden=true;saveState('saved');
  closePicker(false);closeSwitcher(false);switcher.siblings=[];renderSwitcher();
  restorePreferences();updateBundle(data,true);renderReferences();renderParts();scheduleDraw(true);
  pollLater();
  if(task.preview_source==='export'&&data.preview.state==='none'){
    api('/api/tasks/'+task.id+'/preview',{source:'export'})
      .then(()=>{if(epoch===routeVersion)refresh(true).catch(()=>{});})
      .catch(e=>{if(epoch===routeVersion&&!e?.offline)toast(e.message);});
  }else{
    loadPreview(data).catch(e=>{if(epoch===routeVersion&&!e?.offline)toast(e.message);});
  }
}
window.addEventListener('hashchange',guard(route));
document.addEventListener('visibilitychange',()=>{
  syncBackgroundPause(document,currentPage);
  if(document.hidden){clearTimeout(pollTimer);return;}
  if(task)refresh().catch(e=>{if(!e?.offline&&!e?.aborted)toast(e.message);});
});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
// 关窗口前立刻保存：任务页的改动、画布的改动（window-chrome.js 关窗口时会等它们）
(window.__whitebgBeforeClose??=[]).push(()=>flush(),()=>canvasUI.flush());
let calibrationUI;
const lightingUI=installLightingUI({$,getTask:()=>task,getItem:item,getViewer:()=>viewer,requestDraw:()=>scheduleDraw(true),busy:()=>calibrationUI.busy(),
  lightChanged:apply=>{
    beforeLighting??=task.items.map(it=>[it,JSON.stringify(effectiveLighting(task,it))]);
    apply();
    changed(false);scheduleDraw(true);
  }});
const aiSettingsUI=installAISettings({$,api,toast});
const materialsUI=installMaterialsUI({$,api,escape,modal,guard,toast,flush,refresh:()=>refresh(true),
  getTask:()=>task,getBundle:()=>lastBundle,
  calibrationBusy:()=>calibrationUI?.busy(),
  openCalibration:()=>calibrationUI?.open(),
  onUpdated:result=>{
    if(result?.task&&task?.id===result.task.id){
      task=structuredClone(result.task);
      lightingUI.notifyTaskChanged();
    }
    if(lastBundle&&result?.materials_detail){
      lastBundle={...lastBundle,materials_detail:result.materials_detail,task:result.task||lastBundle.task};
    }
    bundleKey=appliedKey=null;
    calibrationUI?.render();
  }});
calibrationUI=installCalibrationUI({$,api,escape,fileUrl,modal,guard,toast,flush,aiSettingsUI,materialsUI,refresh:()=>refresh(true),
  getTask:()=>task,getBundle:()=>lastBundle,prepare:()=>lightingUI.images(),updateLighting:()=>scheduleDraw(true)});
const runnerUI=installRunner({api,toast,refreshTask:()=>refresh(true)});
const settingsUI=installSettings({$,api,toast,guard,escape});
const canvasUI=installCanvas({$,api,toast,toastAction,guard,escape});
setOfflineRetry(async()=>{
  if(task){await refresh(true).catch(()=>{});return;}
  try{
    await api('/api/health');
    if(!$('start-page').hidden)await tasksList().catch(()=>{});
    else if(!$('canvas-page').hidden)canvasUI.retryPoll?.();
    else if(!$('refs-page').hidden)await calibrationUI.showRefs().catch(()=>{});
    else if(!$('settings-page').hidden)await settingsUI.show().catch(()=>{});
  }catch{}
});
try{
  enableDesktop();
  if(!location.hash){
    let last='';try{last=localStorage.getItem('whitebg.page')||'';}catch{}
    if(LAST_PAGES.includes(last))history.replaceState(null,'',last);
  }
  const saved=localStorage.getItem('whitebg.product');
  if(saved){
    $('product-path').value=saved;
    if(!location.hash)try{await scanProduct(saved);}catch(error){$('scan-error').textContent=error.message;}
  }
  await route({skipScan:!!scan&&!location.hash});
}catch(error){toast(error.message);}

// 只读调试入口：便于开发验收检查真实加载状态和取景数据。
window.__whitebg={get task(){return structuredClone(task);},get model(){const m=viewer?.model;return m?{size:m.size,meshCount:m.meshCount,parts:m.parts.map(p=>({name:p.name,type:p.type})),box:m.box}:null;},flush,refresh};
