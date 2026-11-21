// 本机设置页：3ds Max 程序、白底模板、历史白底目录、数据目录、界面动效。
export function installSettings({$,api,toast,guard,escape}){
  let state=null,history=[];
  const bridge=()=>window.pywebview?.api;
  const showDesktop=()=>{
    const available=!!bridge()?.choose_path;
    document.querySelectorAll('[data-desktop-only]').forEach(button=>button.hidden=!available);
  };
  window.addEventListener('pywebviewready',showDesktop);
  window.addEventListener('whitebgdesktopready',showDesktop);

  function status(element,ok,good,bad){
    element.textContent=ok?good:bad;
    element.className='field-status '+(ok?'ok':'bad');
  }
  function renderHistory(){
    const checks=state?.checks?.history_roots||[];
    $('set-history-list').innerHTML=history.map((path,i)=>{
      const known=state?.history_roots?.indexOf(path);
      const missing=known>=0&&checks[known]===false;
      return `<div class="path-item${missing?' missing':''}"><span title="${escape(path)}">${escape(path)}</span><button type="button" data-remove="${i}" aria-label="移除这个目录">移除</button></div>`;
    }).join('');
    $('set-history-list').querySelectorAll('[data-remove]').forEach(button=>button.onclick=()=>{
      history.splice(Number(button.dataset.remove),1);renderHistory();
    });
  }
  function render(){
    $('set-max-exe').value=state.max_exe;
    status($('set-max-status'),state.checks.max_exe,'已找到 3ds Max'+(state.max_year?' '+state.max_year:''),'还没有找到 3ds Max');
    const plugin=state.plugin;
    status($('set-plugin-status'),!!plugin?.installed,'插件已装好：Max 一打开就会自动连上工作台','插件还没装');
    $('set-remove-plugin').hidden=!plugin?.installed;
    history=[...state.history_roots];renderHistory();
    $('set-data-dir').textContent=state.data_dir;$('set-data-dir').title=state.data_dir;
    $('set-motion').checked=document.documentElement.dataset.motion!=='off';
    $('setup-banner').hidden=state.ready;
  }
  async function show(){
    showDesktop();
    state=await api('/api/settings');
    render();
  }
  async function checkReady(){
    try{const data=await api('/api/settings');state=data;$('setup-banner').hidden=data.ready;}
    catch{ $('setup-banner').hidden=true; }
  }
  async function pick(kind,input){
    const path=await bridge()?.choose_path(kind);
    if(path)input.value=path;
    return path;
  }
  $('set-max-browse').onclick=guard(()=>pick('exe',$('set-max-exe')));
  $('set-connect').onclick=guard(async()=>{state=await api('/api/settings/connect-max',{});render();toast(state.message||'已装好');});
  $('set-remove-plugin').onclick=guard(async()=>{state=await api('/api/settings/remove-plugin',{});render();toast('已移除插件');});
  $('set-history-browse').onclick=guard(async()=>{const path=await pick('folder',$('set-history-input'));if(path)addHistory();});
  $('set-max-detect').onclick=guard(async()=>{
    const {candidates}=await api('/api/settings/detect-max',{});
    if(!candidates.length)return toast('没有找到 3ds Max，请手动指定 3dsmax.exe');
    $('set-max-exe').value=candidates[0];
    toast(candidates.length>1?`找到 ${candidates.length} 个版本，已填入最新的`:'已找到 3ds Max');
  });
  function addHistory(){
    const value=$('set-history-input').value.trim().replace(/^"|"$/g,'');
    if(!value)return;
    if(!history.includes(value))history.push(value);
    $('set-history-input').value='';renderHistory();
  }
  $('set-history-add').onclick=addHistory;
  $('set-history-input').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();addHistory();}};
  $('set-open-data').onclick=guard(()=>api('/api/settings/open-data',{}));
  $('set-motion').onchange=()=>{
    const value=$('set-motion').checked?'on':'off';
    document.documentElement.dataset.motion=value;
    try{localStorage.setItem('whitebg.motion',value);}catch{}
  };
  $('set-save').onclick=guard(async()=>{
    const button=$('set-save');button.disabled=true;
    try{
      state=await api('/api/settings',{max_exe:$('set-max-exe').value.trim(),history_roots:history});
      render();$('set-status').textContent='';toast(state.ready?'已保存，可以开始出图了':'已保存');
    }finally{button.disabled=false;}
  });
  return {show,checkReady};
}
