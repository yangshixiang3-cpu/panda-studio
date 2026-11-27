// 生图渠道：一个渠道 = 接口地址 + API Key + 模型 + 接口类型 + 比例的传法 + 额外参数（+ 拉到的模型列表），可以存好几个、改名。
// 不写死任何中转站或模型：不同中转站认的字段不一样，由渠道自己选。
// 这里只管「生图渠道」弹窗：左边渠道列表，右边编辑；新建、保存、复制、删除、拉取模型。
// 改完调 onChange(最新设置)，由 canvas.js 更新顶栏和输入面板。
const host=url=>{try{return new URL(url).host;}catch{return '';}};

export function installChannels({$,api,toast,guard,escape,onChange}){
  const dialog=$('canvas-settings');
  let settings={active:'',channels:[],ai_base_url:''},selected=null,draft=null,pulled=null,dirty=false,pendingSwitch=null,deleteArmed=null;

  const channelById=id=>settings.channels.find(c=>c.id===id)||null;
  // 新渠道默认叫「渠道 N」：N 接着现有「渠道 数字」往下数
  function nextName(){
    const numbers=settings.channels.map(c=>Number(c.name.match(/^渠道 (\d+)$/)?.[1])).filter(Boolean);
    return `渠道 ${Math.max(0,...numbers)+1}`;
  }
  const describe=c=>[c.model||'未选模型',host(c.base_url)||(settings.ai_base_url?'沿用 AI 设置':'')].filter(Boolean).join(' · ');

  function setStatus(text,kind=''){
    const status=$('cvs-status');status.hidden=!text;status.textContent=text||'';status.dataset.kind=kind;
  }
  function renderList(){
    const items=settings.channels.map(c=>`<button type="button" role="option" class="cvc-item${c.id===selected?' current':''}" aria-selected="${c.id===selected}" data-channel="${c.id}">
        <span class="cvc-name">${escape(c.name)}${c.id===settings.active?'<em>正在用</em>':''}</span>
        <small>${escape(describe(c))}</small></button>`);
    if(draft)items.push(`<button type="button" role="option" class="cvc-item draft${selected===null?' current':''}" aria-selected="${selected===null}" data-channel="">
        <span class="cvc-name">${escape($('cvs-name').value.trim()||draft.name)}</span><small>新渠道，还没保存</small></button>`);
    $('cvc-list').innerHTML=items.join('')||'<p class="cvc-empty">还没有渠道</p>';
  }
  function fill(channel){
    const c=channel||draft;
    $('cvs-name').value=c.name||'';
    $('cvs-base').value=c.base_url||'';
    $('cvs-base').placeholder=settings.ai_base_url?`沿用 AI 设置：${settings.ai_base_url}`:'https://example.com/v1';
    $('cvs-key').value='';
    $('cvs-key').placeholder=c.key_source==='own'?`已保存（尾号 ${c.key_tail}），留空不改`:c.key_source==='ai'?`沿用 AI 设置里的 Key（尾号 ${c.key_tail}）`:'粘贴 API Key';
    $('cvs-reuse').checked=c.reuse_ai_key!==false;
    $('cvs-model').value=c.model||'';
    dialog.querySelectorAll('input[name=cvs-mode]').forEach(r=>r.checked=r.value===(c.mode||'auto'));
    $('cvs-extra').value=c.extra&&Object.keys(c.extra).length?JSON.stringify(c.extra):'';
    pulled=Array.isArray(c.models)&&c.models.length?c.models:null;
    renderModels();
    $('cvs-delete').hidden=!channel;$('cvs-copy').hidden=!channel;
    disarmDelete();setStatus('');dirty=false;pendingSwitch=null;
  }
  // 拉到的模型：像生图的排前面，点一下填进「模型」
  function renderModels(){
    const list=pulled||[];
    $('cvs-model-list').innerHTML=list.map(m=>`<option value="${escape(m)}"></option>`).join('');
    const image=list.filter(m=>/image|dall|banana|imagen|flux|seedream|kolors|cogview|wanx|jimeng|mj|midjourney/i.test(m)).slice(0,16);
    const chips=$('cvs-model-chips');chips.hidden=!image.length;
    chips.innerHTML=image.map(m=>`<button type="button" data-model="${escape(m)}" class="${m===$('cvs-model').value.trim()?'active':''}">${escape(m)}</button>`).join('');
  }
  function select(id){
    if(id){selected=id;draft=null;fill(channelById(id));}
    else{selected=null;draft=draft||{name:nextName(),base_url:'',model:'',mode:'auto',reuse_ai_key:true,key_source:'none',models:[],size_style:'auto',extra:{}};fill(null);}
    renderList();
  }
  function values(){
    return {id:selected||undefined,name:$('cvs-name').value.trim(),base_url:$('cvs-base').value.trim(),api_key:$('cvs-key').value.trim(),
      reuse_ai_key:$('cvs-reuse').checked,model:$('cvs-model').value.trim(),
      mode:dialog.querySelector('input[name=cvs-mode]:checked')?.value||'auto',
      extra:$('cvs-extra').value.trim(),...(pulled?{models:pulled}:{})};
  }
  function changed(next){settings=next;onChange?.(settings);}

  async function read(){changed(await api('/api/canvas/settings'));return settings;}
  async function open(id){
    await read();
    dialog.showModal();
    select(id&&channelById(id)?id:settings.active&&channelById(settings.active)?settings.active:null);
    if(!settings.channels.length)setStatus('先配好第一个渠道：填接口地址和 API Key，点「拉取模型」选一个，保存后就叫「渠道 1」（可以改名）。');
  }

  // ── 列表 ──
  $('cvc-list').addEventListener('click',event=>{
    const item=event.target.closest('[data-channel]');if(!item)return;
    const id=item.dataset.channel||null;
    if(id===selected)return;
    // 有没保存的修改：先提示，再点一次才放弃
    if(dirty&&pendingSwitch!==item.dataset.channel){
      pendingSwitch=item.dataset.channel;
      setStatus('这个渠道改了还没保存：点「保存」，或再点一次那个渠道放弃修改。','warn');
      return;
    }
    if(!id)return;
    select(id);
  });
  $('cvc-new').onclick=()=>{
    if(dirty&&selected!==null&&pendingSwitch!=='new'){pendingSwitch='new';setStatus('这个渠道改了还没保存：点「保存」，或再点一次「新建渠道」放弃修改。','warn');return;}
    draft=null;select(null);$('cvs-name').focus();$('cvs-name').select();
  };

  // ── 表单 ──
  $('cvs-form').addEventListener('input',event=>{
    dirty=true;pendingSwitch=null;
    if(event.target.id==='cvs-name'&&!selected)renderList();
    if(event.target.id==='cvs-model')renderModels();
  });
  $('cvs-model-chips').addEventListener('click',event=>{
    const chip=event.target.closest('[data-model]');if(!chip)return;
    $('cvs-model').value=chip.dataset.model;dirty=true;renderModels();
  });
  $('cvs-models').onclick=guard(async()=>{
    const button=$('cvs-models');button.disabled=true;button.textContent='正在拉取…';
    try{
      const {models,image_models}=await api('/api/canvas/models',values());
      pulled=models;dirty=true;renderModels();
      setStatus(`连上了：共 ${models.length} 个模型${image_models.length?`，像生图模型的有 ${image_models.length} 个，点一下选用`:'（没认出生图模型，请手动填写）'}`,'ok');
      if(!$('cvs-model').value&&image_models[0]){$('cvs-model').value=image_models[0];renderModels();}
    }finally{button.disabled=false;button.textContent='拉取模型';}
  });
  $('cvs-form').onsubmit=guard(async event=>{
    event.preventDefault();
    const data=values();
    if(!data.name)data.name=selected?channelById(selected).name:draft.name;
    const result=await api('/api/canvas/settings',data);
    changed(result);draft=null;select(result.saved);
    const saved=channelById(result.saved);
    setStatus(saved.model?`已保存「${saved.name}」`:`已保存「${saved.name}」，还没选模型，生图前要选一个`,saved.model?'ok':'warn');
    toast(`已保存生图渠道「${saved.name}」`);
  });
  $('cvs-copy').onclick=guard(async()=>{
    if(!selected)return;
    const result=await api(`/api/canvas/settings/channels/${selected}/copy`,{});
    changed(result);select(result.saved);
    setStatus('已复制，连 Key 一起。改个名字、换个模型再保存。','ok');
    $('cvs-name').focus();$('cvs-name').select();
  });
  // 删除：点一下变成「再点一次删除」，3 秒内再点才删（Key 也一起删掉）
  function disarmDelete(){clearTimeout(deleteArmed);deleteArmed=null;$('cvs-delete').textContent='删除渠道';$('cvs-delete').classList.remove('armed');}
  $('cvs-delete').onclick=guard(async()=>{
    if(!selected)return;
    if(!deleteArmed){
      $('cvs-delete').textContent='再点一次删除';$('cvs-delete').classList.add('armed');
      deleteArmed=setTimeout(disarmDelete,3000);return;
    }
    disarmDelete();
    const name=channelById(selected).name;
    changed(await api(`/api/canvas/settings/channels/${selected}`,undefined,'DELETE'));
    select(settings.active||null);
    toast(`已删除生图渠道「${name}」`);
  });
  const close=()=>dialog.close();
  $('cvs-cancel').onclick=close;$('cvs-close').onclick=close;

  return {open,read,get settings(){return settings;},
    async activate(id){changed(await api(`/api/canvas/settings/channels/${id}/activate`,{}));return settings;}};
}
