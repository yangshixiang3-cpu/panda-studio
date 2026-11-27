// 画布顶栏：项目名（点开是项目管理）+ 画布标签（切换、新建、双击改名、× 删除可撤销）。
// 空白处可以拖动窗口（window-chrome.js 把 .cv-topbar、.cv-topbar-space 标成拖动区域）。
// 不管画布内容：要切换时调 open(项目, 画布号)，由 canvas.js 去载入。
const fileUrl=(pid,file)=>`/api/canvas/projects/${encodeURIComponent(pid)}/files/${encodeURIComponent(file)}`;
const when=value=>{const d=new Date(value);return Number.isNaN(d)?'':`${d.getMonth()+1}-${String(d.getDate()).padStart(2,'0')} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;};
const TAB_ICON='<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4.5" width="16" height="15" rx="3"/><path d="M9 12h6M12 9v6"/></svg>';
const CLOSE='<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7l10 10M17 7 7 17"/></svg>';

export function installCanvasProjects({$,api,toast,toastAction,guard,escape,open}){
  let projects=[],project=null,canvasId=null,root='';
  const dialog=$('cv-projects');

  function renderBar(){
    $('cv-project').querySelector('span').textContent=project?.name||'项目';
    $('cv-tabs').innerHTML=(project?.canvases||[]).map(c=>`<div class="cv-tab${c.id===canvasId?' active':''}" role="tab" aria-selected="${c.id===canvasId}" tabindex="0" data-tab="${c.id}">
        ${TAB_ICON}<span class="cv-tab-name">${escape(c.name)}</span><button type="button" class="cv-tab-close" data-close="${c.id}" aria-label="删除画布 ${escape(c.name)}" title="删除画布">${CLOSE}</button></div>`).join('');
  }
  async function reload(){
    const data=await api('/api/canvas/projects');
    projects=data.projects;root=data.root;
    if(project)project=projects.find(p=>p.id===project.id)||null;
    return data;
  }
  async function switchTo(pid,cid){
    const target=projects.find(p=>p.id===pid);
    if(!target)throw new Error('没有这个项目');
    const canvas=target.canvases.find(c=>c.id===cid)||target.canvases[0];
    project=target;canvasId=canvas.id;
    renderBar();
    await open(project,canvasId);
    api('/api/canvas/prefs',{last:{project:project.id,canvas:canvasId}}).catch(()=>{});
  }
  async function init(){
    const data=await reload();
    const last=data.prefs?.last;
    const target=projects.find(p=>p.id===last?.project)||projects[0];
    await switchTo(target.id,last?.project===target.id?last.canvas:target.canvases[0].id);
    return data.prefs;
  }

  // ── 标签 ──
  $('cv-tabs').addEventListener('click',guard(async event=>{
    const close=event.target.closest('[data-close]');
    if(close){event.stopPropagation();return removeCanvas(close.dataset.close);}
    const tab=event.target.closest('[data-tab]');
    if(tab&&tab.dataset.tab!==canvasId&&!tab.querySelector('input'))await switchTo(project.id,tab.dataset.tab);
  }));
  $('cv-tabs').addEventListener('keydown',event=>{
    const tab=event.target.closest('[data-tab]');
    if(tab&&event.target===tab&&(event.key==='Enter'||event.key===' ')){event.preventDefault();tab.click();}
  });
  $('cv-tabs').addEventListener('dblclick',event=>{
    const tab=event.target.closest('[data-tab]');if(!tab||event.target.closest('[data-close]'))return;
    rename(tab.querySelector('.cv-tab-name'),async name=>{
      project=await api(`/api/canvas/projects/${project.id}/canvases/${tab.dataset.tab}`,{name},'PUT');await reload();renderBar();
    });
  });
  $('cv-tab-add').onclick=guard(async()=>{
    const {project:updated,canvas}=await api(`/api/canvas/projects/${project.id}/canvases`,{});
    await reload();project=projects.find(p=>p.id===updated.id);
    await switchTo(project.id,canvas.id);
  });
  async function removeCanvas(cid){
    const canvas=project.canvases.find(c=>c.id===cid);
    if(project.canvases.length<=1)return toast('项目里至少要留一张画布');
    await api(`/api/canvas/projects/${project.id}/canvases/${cid}`,undefined,'DELETE');
    await reload();
    if(cid===canvasId)await switchTo(project.id,project.canvases[0].id);else renderBar();
    toast(`已删除画布「${canvas?.name||''}」（在 canvas\\trash 里还能找回）`);
  }
  // 在原位置换成输入框改名：回车或失焦保存，Esc 取消
  function rename(label,save){
    const old=label.textContent,input=document.createElement('input');
    input.className='cv-rename';input.value=old;input.maxLength=40;
    label.replaceWith(input);input.focus();input.select();
    let done=false;
    const finish=async commit=>{
      if(done)return;done=true;
      const name=input.value.trim();
      if(commit&&name&&name!==old){try{await save(name);}catch(error){toast(error.message);renderBar();renderList();}}
      else{renderBar();renderList();}
    };
    input.addEventListener('keydown',event=>{event.stopPropagation();if(event.key==='Enter')finish(true);if(event.key==='Escape')finish(false);});
    input.addEventListener('blur',()=>finish(true));
  }

  // ── 项目管理 ──
  function renderList(){
    $('cvp-list').innerHTML=projects.map(p=>`<article class="cvp-card${p.id===project?.id?' current':''}" data-project="${p.id}" tabindex="0">
        <div class="cvp-thumbs">${p.thumbs.length?p.thumbs.map(f=>`<img src="${fileUrl(p.id,f)}?w=256" alt="" loading="lazy">`).join(''):'<span>还没有图片</span>'}</div>
        <div class="cvp-meta"><strong class="cvp-name">${escape(p.name)}</strong><small>${p.canvases.length} 张画布 · ${p.image_count} 张图 · ${when(p.updated)}</small></div>
        <div class="cvp-tools">
          <button type="button" data-act="rename" title="重命名">改名</button>
          <button type="button" data-act="folder" title="在资源管理器里打开这个项目的图片文件夹">文件夹</button>
          <button type="button" data-act="delete" title="删除项目（可撤销）">删除</button></div>
      </article>`).join('');
  }
  async function showProjects(){await reload();renderList();if(!dialog.open)dialog.showModal();}
  $('cv-project').onclick=guard(showProjects);
  $('cvp-close').onclick=()=>dialog.close();
  $('cvp-folder').onclick=guard(()=>api('/api/canvas/reveal',{}));
  $('cvp-new').onclick=guard(async()=>{
    const created=await api('/api/canvas/projects',{name:''});
    await reload();dialog.close();
    await switchTo(created.id,created.canvases[0].id);
    toast('已新建项目，点左上角项目名可以改名');
  });
  $('cvp-list').addEventListener('click',guard(async event=>{
    const card=event.target.closest('[data-project]');if(!card)return;
    const pid=card.dataset.project,act=event.target.closest('[data-act]')?.dataset.act,target=projects.find(p=>p.id===pid);
    if(act==='rename')return rename(card.querySelector('.cvp-name'),async name=>{
      await api(`/api/canvas/projects/${pid}`,{name},'PUT');await reload();renderBar();renderList();
    });
    if(act==='folder')return api('/api/canvas/reveal',{project:pid});
    if(act==='delete'){
      if(projects.length<=1)return toast('至少要留一个项目');
      await api(`/api/canvas/projects/${pid}`,undefined,'DELETE');
      await reload();renderList();
      if(pid===project.id)await switchTo(projects[0].id,projects[0].canvases[0].id);
      return toastAction(`已删除项目「${target.name}」`,'撤销',async()=>{
        await api(`/api/canvas/projects/${pid}/restore`,{});await reload();renderList();
      });
    }
    if(!event.target.closest('input')){dialog.close();if(pid!==project.id)await switchTo(pid,target.canvases[0].id);}
  }));
  $('cvp-list').addEventListener('keydown',event=>{
    const card=event.target.closest('[data-project]');
    if(card&&event.target===card&&event.key==='Enter')card.click();
  });

  return {init,current:()=>({project,canvasId}),reload,renderBar};
}
