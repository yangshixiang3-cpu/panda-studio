import {previewAttributes} from './lightbox.js';
import {cropUrlPath,installAnnotator,regionStyle} from './annotate.js';
import {finishColorOf} from './finishes.js';
// 调整名称只在内部使用；页面显示日常中文。
export function describeOverrides(overrides){
  if(!overrides)return [];
  const lines=[];
  const percent=(value,word)=>`${word}${value>=1?'增加':'减少'} ${Math.round(Math.abs(value-1)*100)}%`;
  for(const [name,v] of Object.entries(overrides.materials||{})){
    const changes=[];
    if(v.hue_shift_deg)changes.push(`色调微调 ${v.hue_shift_deg>0?'+':''}${v.hue_shift_deg}°`);
    for(const [key,label] of [['saturation_mul','颜色浓淡'],['brightness_mul','明亮程度'],['contrast_mul','明暗反差'],['reflection_mul','反光强度'],['refraction_mul','玻璃折射'],['opacity_mul','玻璃不透明程度']]){
      if(v[key]!=null&&v[key]!==1)changes.push(percent(v[key],label));
    }
    if(v.glossiness_delta)changes.push(v.glossiness_delta>0?'表面更光亮':'表面更柔和');
    if(v.ior!=null)changes.push(`玻璃折射调整为 ${v.ior}`);
    if(changes.length)lines.push({target:name,change:changes.join('，')});
  }
  const light=overrides.lights||{};
  if(light.intensity_mul!=null&&light.intensity_mul!==1)lines.push({target:'灯光',change:percent(light.intensity_mul,'亮度')});
  if(light.rotate_deg)lines.push({target:'灯光',change:`方向转动 ${light.rotate_deg}°`});
  for(const [name,v] of Object.entries(light.per_light||{}))if(v.intensity_mul!==1)lines.push({target:name,change:percent(v.intensity_mul,'灯光亮度')});
  const reflection=overrides.environment?.reflection_strength_mul;
  if(reflection!=null&&reflection!==1)lines.push({target:'环境反光',change:percent(reflection,'强度')});
  for(const [no,v] of Object.entries(overrides.items||{}))if(v.lights?.rotate_deg)lines.push({target:`第 ${no} 张主图`,change:`光照方向转动 ${v.lights.rotate_deg}°`});
  return lines;
}

export function describeUsage(usage){
  if(!usage)return '';
  const tokens=usage.total_tokens??(usage.prompt_tokens!=null&&usage.completion_tokens!=null
    ?usage.prompt_tokens+usage.completion_tokens:null);
  const parts=[usage.model||'未选择模型',new Intl.NumberFormat('zh-CN',{maximumFractionDigits:2}).format(usage.latency_sec)+' 秒'];
  if(tokens!=null)parts.push((tokens>=1000?(tokens/1000).toFixed(1).replace(/\.0$/,'')+'k':tokens)+' tokens');
  else{
    if(usage.prompt_tokens!=null)parts.push('输入 '+usage.prompt_tokens+' tokens');
    if(usage.completion_tokens!=null)parts.push('输出 '+usage.completion_tokens+' tokens');
  }
  return parts.join(' · ');
}

export function installCalibrationUI({$,api,escape,fileUrl,modal,guard,toast,flush,aiSettingsUI,refresh,getTask,getBundle,prepare,updateLighting}){
  let signature='',sending=false,startUnavailable=false;
  const annotator=installAnnotator({$,api,escape,fileUrl});
  let editingTaskRefs=false;
  const calibration=()=>getBundle()?.task?.ai_calibration??getTask()?.ai_calibration??{};
  const calibrationOpen=()=>$('dialog').open&&$('dialog').dataset.view==='calibration';
  const refEndpoint=id=>'/api/tasks/'+id+'/calibration-refs/';
  const refPath=entry=>getBundle().task_dir+'/'+entry.file;
  const busy=()=>['rendering','thinking'].includes(calibration().state)
    ||['calibration','ai_calibration'].includes(getBundle()?.progress?.kind);
  const roundImages=r=>{
    const bundle=getBundle();
    if(bundle?.available_rounds&&!bundle.available_rounds.includes(r.n))return [];
    const job=bundle?.jobs?.find(j=>j.job_id===r.job_id);
    return (job?.items||[]).filter(i=>i.state==='done').map(i=>({no:i.no,path:i.out_image||`${bundle.task_dir}/calibration/round-${r.n}/smallshots/${i.no}.jpg`}));
  };
  function renderHistory(){
    const c=calibration(),available=getBundle()?.available_rounds;
    const rounds=(c.rounds||[]).filter(r=>!available||available.includes(r.n));
    const open=calibrationOpen();
    const previousNote=open?$('calibration-note')?.value:undefined;
    const note=previousNote??c.user_note??'';
    const active=busy(),disabled=active||sending?'disabled':'';
    const content=`<div class="calibration-engine"><span id="calibration-engine">${escape(aiSettingsUI.description())}</span><button id="calibration-settings">设置</button></div><div class="calibration-followup"><label for="calibration-note">一句话要求（可选）</label><input type="text" id="calibration-note" maxlength="2000" value="${escape(note)}"><div class="dialog-actions">${rounds.length?`<button id="undo-calibration" ${c.accepted_round==null?'disabled':disabled}>撤销 AI 调整</button><button id="retry-calibration" ${disabled}>再调一轮</button>`:''}${active?`<span class="is-busy">AI 第 ${c.round??getBundle()?.progress?.round??0} 轮</span>`:`<button id="start-calibration" class="primary" ${sending||startUnavailable?'disabled':''}>${startUnavailable?'需要更新服务端':'开始 AI 校准'}</button>`}</div></div>${c.state==='failed'&&(!available||rounds.length)?`<p class="error">${escape(rounds.at(-1)?.error||'AI 校准失败，可直接用当前小样。')}</p>`:''}<div class="calibration-rounds">${rounds.map(r=>{
      const changes=describeOverrides(r.overrides),images=roundImages(r),accepted=c.accepted_round===r.n,usage=describeUsage(r.usage);
      return `<section class="calibration-round ${accepted?'accepted':''}"><div class="round-heading"><h3>第 ${r.n} 轮${accepted?' · 已采用':''}</h3><span class="confidence">置信度 ${r.confidence==null?'待分析':Math.round(r.confidence*100)+'%'}</span></div>${usage?`<p class="round-usage">${escape(usage)}</p>`:'' }<div class="round-images">${images.map(i=>`<figure><img ${previewAttributes(fileUrl(i.path),`第 ${r.n} 轮 · ${String(i.no).padStart(2,'0')} · ${getTask().items.find(it=>it.no===i.no)?.label||'小样'}`,'calibration-rounds')} src="${fileUrl(i.path)}" alt="第 ${r.n} 轮第 ${i.no} 张小样"><figcaption>第 ${i.no} 张</figcaption></figure>`).join('')||'<p class="hint">正在等待本轮小样。</p>'}</div><p class="round-summary">${escape(r.summary||(r.error?'本轮分析未完成，可查看已出的小样。':'等待小样与分析…'))}</p>${r.error?`<p class="error">${escape(r.error)}</p>`:''}<ul class="round-changes">${changes.map(v=>`<li><strong>${escape(v.target)}</strong> → ${escape(v.change)}</li>`).join('')||'<li>本轮未增加 AI 调整。</li>'}</ul><div class="dialog-actions"><button data-accept-round="${r.n}" ${!images.length?'disabled':disabled}>${accepted?'已用这一轮':'用这一轮'}</button></div></section>`;
    }).join('')||`<div class="empty">${available?'校准记录已清理':'暂无校准记录'}</div>`}</div>`;
    const prior=$('dialog'),scroll=prior.scrollTop,noteFocused=document.activeElement===$('calibration-note');
    const caret=noteFocused?[$('calibration-note').selectionStart,$('calibration-note').selectionEnd]:null;
    modal('AI 校准',content);prior.dataset.view='calibration';
    renderTaskRefs();
    prior.querySelectorAll('.round-heading').forEach((heading,index)=>{
      const used=rounds[index].task_refs_used;
      if(!Array.isArray(used)||!used.length)return;  // 没用到本任务参考时不显示
      const usage=document.createElement('p');
      usage.className='round-usage';
      const count=new Set(used.map(ref=>ref.id)).size;
      usage.textContent=`用了 ${count} 张本任务参考`;
      heading.after(usage);
    });
    if(open)prior.scrollTop=scroll;
    if(caret){$('calibration-note').focus();$('calibration-note').setSelectionRange(...caret);}
    $('dialog-content').querySelectorAll('[data-accept-round]').forEach(b=>b.onclick=guard(()=>action({action:'accept',round:Number(b.dataset.acceptRound)})));
    $('calibration-settings').onclick=aiSettingsUI.open;
    if($('undo-calibration'))$('undo-calibration').onclick=guard(()=>action({action:'undo'}));
    if($('retry-calibration'))$('retry-calibration').onclick=guard(()=>action({action:'retry',user_note:$('calibration-note').value}));
    if($('start-calibration'))$('start-calibration').onclick=guard(()=>action({action:'start',user_note:$('calibration-note').value}));
  }
  function renderTaskRefs(){
    const refs=calibration().task_refs||[];
    const names=[...new Set(refs.flatMap(entry=>entry.regions.map(region=>region.material)))];
    $('dialog-content').querySelector('.calibration-followup').insertAdjacentHTML('beforebegin',`
      <section class="calibration-refs" aria-label="材质参考">
        <h3>材质参考 <small class="hint">${refs.length?`本任务 ${refs.length} 张，AI 校准时优先参考`:'可选：给 AI 指定某种材质该是什么样'}</small></h3>
        <div class="calibration-ref-list">${refs.map(entry=>`<div class="calibration-ref-card">
          <button type="button" class="annotation-preview" data-calibration-ref="${entry.id}" title="${escape(entry.name)}" aria-label="${escape(entry.name)}">
            <span class="annotation-preview-fit"><img src="${fileUrl(refPath(entry),'')}" alt="" draggable="false">
            ${entry.regions.map(region=>`<span class="annotation-outline" style="${regionStyle(region.box,region.material,names)}"></span>`).join('')}</span>
          </button><button type="button" class="calibration-ref-remove" data-remove-ref="${entry.id}" aria-label="删除 ${escape(entry.name)}">×</button>
        </div>`).join('')}
        <button type="button" id="calibration-ref-library" class="calibration-ref-pick" title="从内置和已认可的材质参考里点选">从参考库选…</button>
        <button type="button" id="calibration-ref-add">添加并标注…</button>
        <input type="file" id="calibration-ref-input" multiple accept=".jpg,.jpeg,.png,.webp" hidden>
        </div>
      </section>`);
    $('calibration-ref-library').onclick=guard(openLibraryPicker);
    $('calibration-ref-add').onclick=()=>$('calibration-ref-input').click();
    $('calibration-ref-input').onchange=guard(event=>{
      const inputs=[...event.target.files].map(file=>({file}));
      event.target.value='';
      return editTaskRefs(inputs);
    });
    for(const entry of refs){
      $('dialog').querySelector(`[data-calibration-ref="${entry.id}"]`).onclick=guard(()=>editTaskRefs([{entry}]));
      $('dialog').querySelector(`[data-remove-ref="${entry.id}"]`).onclick=guard(()=>deleteTaskRef(entry));
    }
  }
  // ── 从参考库点选材质参考：按材质列出内置和已认可的图，可多选；选好后逐张进入框选，材质名预填为所属材质。
  const pick={selected:new Map(),material:'',groups:[]};
  const fileName=source=>String(source||'').replaceAll('\\','/').split('/').pop();
  function pickDialog(){
    let dialog=$('ref-pick-dialog');
    if(dialog)return dialog;
    document.body.insertAdjacentHTML('beforeend',`<dialog id="ref-pick-dialog" class="ref-pick" aria-labelledby="ref-pick-title">
      <header class="ref-pick-head"><div><h2 id="ref-pick-title">从参考库选材质参考</h2>
        <p class="hint">点图选中，可以跨材质多选。选好后逐张框出图里对应材质的位置，材质名已帮你填好。</p></div>
        <button type="button" class="ref-pick-close" data-pick-cancel aria-label="关闭">×</button></header>
      <div class="ref-pick-body"><nav class="ref-pick-materials" role="tablist" aria-label="材质"></nav>
        <div class="ref-pick-grid" role="listbox" aria-multiselectable="true"></div></div>
      <footer class="ref-pick-foot"><span class="ref-pick-count"></span><span class="ref-pick-tip">想加别的图：点「添加并标注…」，或到「参考库」页在对应材质下添加</span>
        <button type="button" data-pick-cancel>取消</button><button type="button" class="primary" data-pick-next>下一步：框选材质</button></footer>
    </dialog>`);
    dialog=$('ref-pick-dialog');
    dialog.querySelectorAll('[data-pick-cancel]').forEach(b=>b.onclick=()=>dialog.close());
    dialog.querySelector('.ref-pick-materials').onclick=e=>{const tab=e.target.closest('[data-material]');if(tab){pick.material=tab.dataset.material;renderPick();}};
    dialog.querySelector('.ref-pick-grid').onclick=e=>{
      const card=e.target.closest('[data-file]');if(!card)return;
      const file=card.dataset.file;
      if(pick.selected.has(file))pick.selected.delete(file);
      else pick.selected.set(file,{file,material:pick.material,source:card.dataset.source});
      renderPick();
    };
    dialog.querySelector('[data-pick-next]').onclick=guard(confirmPick);
    return dialog;
  }
  function renderPick(){
    const dialog=pickDialog(),selected=[...pick.selected.values()];
    dialog.querySelector('.ref-pick-materials').innerHTML=pick.groups.map(([key,list])=>{
      const color=finishColorOf(key),chosen=selected.filter(s=>s.material===key).length;
      return `<button type="button" role="tab" data-material="${escape(key)}" aria-selected="${key===pick.material}">
        <span class="ref-pick-swatch" style="background:${color!==null?'#'+color.toString(16).padStart(6,'0'):'var(--surface-3)'}"></span>
        <span class="ref-pick-name">${escape(key)}</span><span class="ref-pick-num">${chosen?`<b>${chosen}</b>/`:''}${list.length}</span></button>`;
    }).join('');
    const list=pick.groups.find(([key])=>key===pick.material)?.[1]||[];
    dialog.querySelector('.ref-pick-grid').innerHTML=list.map(entry=>{
      const on=pick.selected.has(entry.file);
      return `<button type="button" class="ref-pick-card" role="option" aria-selected="${on}" data-file="${escape(entry.file)}" data-source="${escape(entry.source||'')}">
        <img loading="lazy" src="${fileUrl(entry.file,'')}" alt="${escape(pick.material)}参考图">
        ${entry.builtin?'<span class="ref-pick-builtin">内置</span>':''}<span class="ref-pick-check" aria-hidden="true"></span>
        <span class="ref-pick-caption" title="${escape(entry.source||'')}">${escape(fileName(entry.source||entry.file))}</span></button>`;
    }).join('')||'<p class="hint">这个材质还没有认可的参考图。</p>';
    dialog.querySelector('.ref-pick-count').textContent=selected.length?`已选 ${selected.length} 张`:'还没选';
    const next=dialog.querySelector('[data-pick-next]');
    next.disabled=!selected.length;next.textContent=selected.length?`下一步：框选材质（${selected.length}）`:'下一步：框选材质';
  }
  async function openLibraryPicker(){
    if(editingTaskRefs||!calibrationOpen())return;
    const library=await api('/api/refs');
    pick.groups=Object.entries(library.materials)
      .map(([key,group])=>[key,group.images.filter(image=>image.status==='approved')]).filter(([,list])=>list.length);
    if(!pick.groups.length)return toast('参考库里还没有认可的材质图，可以先到「参考库」页添加。');
    pick.selected.clear();
    // 默认打开和场景材质同名的分组（如果有）
    const scene=getBundle()?.scene_materials||[];
    pick.material=(pick.groups.find(([key])=>scene.some(name=>name.includes(key)))||pick.groups[0])[0];
    renderPick();pickDialog().showModal();
  }
  async function confirmPick(){
    const dialog=pickDialog(),chosen=[...pick.selected.values()];
    if(!chosen.length)return;
    const next=dialog.querySelector('[data-pick-next]');next.disabled=true;next.textContent='正在取图…';
    try{
      const inputs=[];
      for(const {file,material,source} of chosen){
        const response=await fetch(fileUrl(file,''));
        if(!response.ok)throw new Error('参考图读取失败：'+fileName(source||file));
        const blob=await response.blob();
        inputs.push({file:new File([blob],fileName(source||file),{type:blob.type||'image/jpeg'}),material});
      }
      dialog.close();
      await editTaskRefs(inputs);
    }finally{renderPick();}
  }
  async function deleteTaskRef(entry){
    const dialog=$('calibration-ref-delete'),id=getTask().id;
    $('calibration-ref-delete-name').textContent=entry.name;
    dialog.returnValue='';
    const closed=new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue),{once:true}));
    dialog.showModal();
    if(await closed!=='delete')return;
    await api(refEndpoint(id)+entry.id,undefined,'DELETE');
    if(getTask()?.id===id)await refresh();
  }
  async function editTaskRefs(inputs){
    if(editingTaskRefs||!inputs.length||!calibrationOpen())return;
    const id=getTask().id,active=()=>getTask()?.id===id&&calibrationOpen();
    editingTaskRefs=true;
    try{
      const {materials}=await api('/api/refs');
      for(const input of inputs){
        if(!active())break;
        try{
          let {entry,file,source,material}=input;
          if(source){
            const response=await fetch('/api/refs/annotated/source',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source})});
            if(!response.ok)throw new Error((await response.json()).error);
            file=await response.blob();
          }
          if(!entry){
            if(file.size>50*1024*1024)throw new Error('单张图片不能超过 50 MB。');
            if(!active())break;
            const name=source?source.replaceAll('\\','/').split('/').pop():file.name;
            const response=await fetch(refEndpoint(id)+'upload?'+new URLSearchParams({name}),{method:'POST',body:file});
            const result=await response.json();
            if(!response.ok)throw new Error(result.error||'图片上传失败。');
            entry=result.entry;
            if(active())await refresh();
          }
          if(!active())break;
          let savedToLibrary=false;
          const newMaterials=(calibration().task_refs||[]).flatMap(ref=>ref.regions
            .filter(region=>region.material_source==='new').map(region=>region.material));
          const result=await annotator.open(entry,materials,-1,null,{
            path:refPath(entry),sceneMaterials:getBundle().scene_materials||[],newMaterials,defaultMaterial:material,
            save:async(regions,in_library)=>{
              await api(refEndpoint(id)+entry.id,{regions,in_library},'PUT');
              savedToLibrary=in_library;
            }
          });
          if(result==='changed'&&active()){
            await refresh();
            if(savedToLibrary)toast('已加入参考库。场景材质和新材质的框只用于本任务。');
          }
        }catch(error){toast(error.message);}
      }
    }finally{editingTaskRefs=false;}
  }
  async function action(body){
    if(sending)return;
    sending=true;
    const id=getTask().id,restart=['start','retry'].includes(body.action);
    const open=()=>$('dialog').open&&$('dialog').dataset.view==='calibration'&&getTask()?.id===id;
    try{
      if(open())renderHistory();
      await flush();
      if(restart){await prepare();await flush();}
      if(getTask()?.id!==id)return;
      await api('/api/tasks/'+id+'/calibration',body);
      if(restart&&open())$('dialog').close();
      await refresh();
      toast(body.action==='accept'?'已采用这一轮':body.action==='undo'?'已撤销 AI 调整':'已开始 AI 校准');
    }catch(error){
      if(body.action==='start'&&(error.status===404||(error.status===400&&error.message.includes('校准操作无效')))){
        startUnavailable=true;toast('需要更新服务端');return;
      }
      throw error;
    }finally{
      sending=false;
      if(open())renderHistory();
    }
  }
  function render(){
    const c=calibration(),active=busy(),button=$('calibration-history');
    const text=active?`AI 第 ${c.round??getBundle()?.progress?.round??0} 轮`:'AI 校准';
    if(button.textContent!==text)button.textContent=text;
    button.classList.toggle('is-busy',active);
    $('items').classList.toggle('calibrating',active);
    $('tab-lighting').classList.toggle('calibrating',active);
    $('accepted-dot').hidden=c.accepted_round==null;
    $('accepted-dot').title=c.accepted_round==null?'':`已采用第 ${c.accepted_round} 轮调整`;
    if($('lighting-disk').getAttribute('aria-disabled')!==String(active)&&!$('lighting-panel').hidden)updateLighting();
    if($('dialog').open&&$('dialog').dataset.view==='calibration'){
      const current=JSON.stringify([c,getBundle()?.jobs,getBundle()?.available_rounds,getBundle()?.scene_materials]);
      if(current!==signature){signature=current;renderHistory();}
    }
  }
  $('calibration-history').onclick=()=>{
    renderHistory();
    aiSettingsUI.refresh().catch(error=>{
      const label=$('calibration-engine');
      if(label)label.textContent='无法读取 AI 设置';
      if(error.status&&error.status!==404)toast(error.message);
    });
  };
  return {render,busy,calibrationOpen,
    dropTaskFiles:files=>editTaskRefs(files.map(file=>({file}))),
    dropTaskPaths:paths=>editTaskRefs(paths.map(source=>({source}))),
    ...installReferenceLibrary({$,api,escape,fileUrl,modal,guard,toast,annotator})};
}

function installReferenceLibrary({$,api,escape,fileUrl,modal,guard,toast,annotator}){
  const groupsElement=$('refs-groups'),annotatedGrid=$('annotated-grid');
  const sourceName=source=>source.replaceAll('\\','/').split('/').pop();
  const statusText={candidate:'待核对',approved:'已认可',rejected:'已不用'};
  let library,activeGroup=null,editing=false;
  const rememberDropTarget=target=>{activeGroup=target?.closest?.('[data-ref-group]')?.dataset.refGroup??null;};
  $('refs-page').onpointermove=event=>rememberDropTarget(event.target);

  function keywordDialog(name){
    modal('编辑关键词 · '+name,`<form id="ref-keywords-form"><label for="ref-keywords">关键词（逗号分隔）</label>
      <input id="ref-keywords" type="text" value="${escape(library.materials[name].keywords.join('，'))}">
      <div class="dialog-actions"><button class="primary">保存关键词</button></div></form>`);
    $('ref-keywords').focus();
    $('ref-keywords-form').onsubmit=guard(async event=>{
      event.preventDefault();
      const keywords=$('ref-keywords').value.split(/[,，]/).map(word=>word.trim()).filter(Boolean);
      await api('/api/refs/materials',{name,keywords});
      $('dialog').close();
      await showRefs();
    });
  }
  function renderAnnotated(materials){
    $('annotated-count').textContent=library.annotated.length+' 张';
    annotatedGrid.innerHTML=library.annotated.map((entry,index)=>`<button type="button" class="ref-card annotation-card" data-annotation="${index}">
      <span class="annotation-preview"><span class="annotation-preview-fit"><img src="${fileUrl(entry.file,'')}" alt="">
      ${entry.regions.map(region=>`<span class="annotation-outline" style="${regionStyle(region.box,region.material,materials)}"></span>`).join('')}</span></span>
      <span class="annotation-caption" title="${escape(entry.source)}">${escape(sourceName(entry.source))}</span><small>${entry.regions.length} 个框</small>
      </button>`).join('');
  }
  async function showRefs(){
    library=await api('/api/refs');
    library.annotated??=[];
    const materials=Object.keys(library.materials);
    renderAnnotated(materials);
    const groups=[...materials.map(key=>[key,library.materials[key].images]),['style',library.style]];
    const crops=new Map(materials.map(key=>[key,[]]));
    library.annotated.forEach((entry,index)=>entry.regions.forEach((region,regionIndex)=>{
      crops.get(region.material)?.push(`<button type="button" class="ref-card approved annotation-card" data-annotation="${index}" data-annotation-region="${regionIndex}">
        <span class="annotation-badge">标注</span><img loading="lazy" src="${fileUrl(cropUrlPath(entry.file,regionIndex),'')}&v=${encodeURIComponent(JSON.stringify(region.box))}" alt="${escape(region.material)}裁图">
        <span class="annotation-caption" title="${escape(entry.source)}">${escape(sourceName(entry.source))}</span></button>`);
    }));
    groupsElement.innerHTML=groups.map(([key,images])=>{
      const label=key==='style'?'白底风格':escape(key),annotated=crops.get(key)||[];
      const count=images.length+annotated.length,approved=images.filter(i=>i.status==='approved').length+annotated.length;
      const cards=images.map(entry=>`<article class="ref-card ${entry.status}">
        ${entry.builtin?'<span class="annotation-badge builtin-badge" title="随软件内置，升级时会自动补齐">内置</span>':''}
        <img ${previewAttributes(fileUrl(entry.file,''),sourceName(entry.source),`library-${key}`)} loading="lazy" src="${fileUrl(entry.file,'')}" alt="${label}参考图">
        <p class="hint" title="${escape(entry.source)}">${escape(sourceName(entry.source))}</p>
        <div class="ref-card-actions"><span>${statusText[entry.status]}</span>
          <button data-ref-status="approved" data-file="${escape(entry.file)}" ${entry.status==='approved'?'disabled':''}>认可</button>
          <button data-ref-status="rejected" data-file="${escape(entry.file)}" ${entry.status==='rejected'?'disabled':''}>不用</button>
        </div></article>`);
      return `<section class="ref-group" data-ref-group="${escape(key)}">
        <div class="section-heading"><div class="ref-group-title"><h2>${label} <small>${count} 张 · 认可 ${approved} 张</small></h2>
          ${key==='style'?'':`<button class="ref-keywords" data-keywords="${escape(key)}" title="${escape(library.materials[key].keywords.join('，'))}">关键词</button>`}</div>
          <label class="upload-button">添加图片…<input type="file" data-ref-upload="${escape(key)}" multiple accept=".jpg,.jpeg,.png,.webp" hidden></label></div>
        <div class="ref-path-row"><input type="text" data-ref-path="${escape(key)}" placeholder="图片完整路径"><button data-add-ref-path="${escape(key)}">添加路径</button></div>
        <div class="ref-grid">${[...annotated,...cards].join('')}</div></section>`;
    }).join('');
    $('refs-page').querySelectorAll('[data-annotation]').forEach(button=>button.onclick=guard(()=>{
      const {annotation,annotationRegion}=button.dataset;
      return openAnnotations([{entry:library.annotated[Number(annotation)],initial:Number(annotationRegion??-1)}]);
    }));
    groupsElement.querySelectorAll('[data-keywords]').forEach(button=>button.onclick=()=>keywordDialog(button.dataset.keywords));
    groupsElement.querySelectorAll('[data-ref-status]').forEach(b=>b.onclick=guard(async()=>{await api('/api/refs/status',{key:b.closest('[data-ref-group]').dataset.refGroup,file:b.dataset.file,status:b.dataset.refStatus});await showRefs();}));
    groupsElement.querySelectorAll('[data-add-ref-path]').forEach(b=>b.onclick=guard(()=>{
      const key=b.dataset.addRefPath;
      const input=b.parentElement.querySelector('[data-ref-path]');
      return addPaths(key,input.value.split(/\r?\n/).map(s=>s.trim().replace(/^"|"$/g,'')).filter(Boolean));
    }));
    groupsElement.querySelectorAll('[data-ref-upload]').forEach(input=>input.onchange=guard(async()=>{await upload(input.dataset.refUpload,[...input.files]);}));
    groupsElement.querySelectorAll('[data-ref-group]').forEach(group=>{
      group.ondragover=e=>{e.preventDefault();activeGroup=group.dataset.refGroup;group.classList.add('drag-over');};
      group.ondragleave=()=>group.classList.remove('drag-over');
    });
  }
  async function openAnnotations(inputs){
    if(editing||!inputs.length)return;
    editing=true;
    try{
      if(!library)await showRefs();
      for(const {entry,file,source,initial} of inputs){
        if(location.hash!=='#refs')break;
        try{
          let blob=file;
          if(source){
            const response=await fetch('/api/refs/annotated/source',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source})});
            if(!response.ok)throw new Error((await response.json()).error);
            blob=await response.blob();
          }
          if(blob?.size>50*1024*1024)throw new Error('单张图片不能超过 50 MB。');
          if(location.hash!=='#refs')break;
          await annotator.open(entry||{source:source||file.name},library.materials,initial,blob);
          if(location.hash==='#refs')await showRefs();
        }catch(error){toast(error.message);}
      }
    }finally{editing=false;}
  }
  $('annotate-add').onclick=()=>$('annotate-input').click();
  $('annotate-input').onchange=guard(event=>{
    const inputs=[...event.target.files].map(file=>({file}));
    event.target.value='';
    return openAnnotations(inputs);
  });
  async function addPaths(key,paths){if(!paths.length)return;await api('/api/refs/images',{key,paths});await showRefs();toast(`已为${key==='style'?'白底风格':key}添加参考图。`);}
  async function upload(key,files){
    for(const file of files){
      if(file.size>50*1024*1024)throw new Error('单张图片不能超过 50 MB。');
      const response=await fetch('/api/refs/upload?'+new URLSearchParams({key,name:file.name}),{method:'POST',body:file});
      const result=await response.json();if(!response.ok)throw new Error(result.error||'图片上传失败。');
    }
    await showRefs();toast('图片已加入参考库，请核对后认可。');
  }
  async function handleDrop(event){
    const group=event.target.closest('[data-ref-group]');
    group?.classList.remove('drag-over');rememberDropTarget(event.target);
    const files=[...event.dataTransfer.files];
    const paths=event.dataTransfer.getData('text/plain').split(/\r?\n/).map(s=>s.trim().replace(/^"|"$/g,'')).filter(Boolean);
    if(!group)return openAnnotations(files.length?files.map(file=>({file})):paths.map(source=>({source})));
    if(files.length)await upload(group.dataset.refGroup,files);
    else await addPaths(group.dataset.refGroup,paths);
  }
  function addDesktopPaths(paths){
    groupsElement.querySelectorAll('.drag-over').forEach(el=>el.classList.remove('drag-over'));
    return activeGroup?addPaths(activeGroup,paths):openAnnotations(paths.map(source=>({source})));
  }
  return {showRefs,handleDrop,addDesktopPaths,rememberDropTarget};
}
