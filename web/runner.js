export function installRunner({api,toast,refreshTask}){
  let status=null,starting=false,deadline,homeRequest,signature;
  const chips=[...document.querySelectorAll('[data-runner]')].map(host=>{
    host.className='runner-status';
    host.innerHTML='<button class="runner-chip" type="button" aria-expanded="false"></button><div class="popover runner-panel" hidden><p class="runner-message"></p><button class="primary runner-start" hidden>启动 Max</button><button class="runner-copy" hidden>复制脚本路径</button></div>';
    const chip=host.querySelector('.runner-chip'),panel=host.querySelector('.runner-panel');
    const message=host.querySelector('.runner-message'),start=host.querySelector('.runner-start'),copy=host.querySelector('.runner-copy');
    const open=value=>{panel.hidden=!value;chip.setAttribute('aria-expanded',String(value));};
    chip.onclick=()=>{if(status?.online===false)open(panel.hidden);};
    start.onclick=async()=>{
      if(starting)return;
      starting=true;render();
      deadline=setTimeout(()=>{
        starting=false;render();toast('Max 启动较慢或启动失败，请查看 Max 窗口');
      },180000);
      try{await api('/api/runner/start',{});}
      catch(error){clearTimeout(deadline);starting=false;render();toast(error.message);return;}
      (host.closest('#task-page')?refreshTask():readHome()).catch(error=>toast(error.message));
    };
    copy.onclick=async()=>{
      try{
        const {root}=await api('/api/health');
        if(!root)throw new Error('无法获取执行器脚本路径。');
        await navigator.clipboard.writeText(root.replace(/[\\/]+$/,'').replaceAll('/','\\')+'\\max\\启动白底执行器.ms');
        toast('已复制脚本路径');
      }catch(error){toast(error.message);}
    };
    document.addEventListener('click',event=>{if(!host.contains(event.target)&&!panel.hidden)open(false);});
    host.addEventListener('keydown',event=>{if(event.key==='Escape'&&!panel.hidden){open(false);chip.focus();}});
    return {host,chip,message,start,copy,open};
  });
  function render(){
    const current=JSON.stringify([status?.online,status?.can_start,status?.max_running,status?.max_windows,status?.max_hidden,starting]);
    if(signature===current)return;
    signature=current;
    const online=status?.online===true,offline=status?.online===false;
    // 只有用户看得见的 Max 才算「已打开」；后台隐藏的 Max（建模等自动化在用）只做提示，不影响启动
    const title=(status?.max_windows||[])[0]||'';
    const hiddenNote=status?.max_hidden?`（后台另有 ${status.max_hidden} 个隐藏的 Max，是其他自动化在用，不影响。）`:'';
    for(const {host,chip,message,start,copy,open} of chips){
      host.dataset.state=online?'online':offline?'offline':'unknown';
      chip.textContent=online?'Max':starting?'正在启动 Max…':offline?(status?.max_running?'Max 已开 · 未连接':'Max 未连接'):'Max 状态未知';
      chip.disabled=!offline;
      if(!offline)open(false);
      message.textContent=starting?'正在启动 Max…'
        :status?.max_running?`开着的 Max「${title.replace(/\s*-\s*Autodesk 3ds Max.*$/,'')||'3ds Max'}」没有加载白底执行器（多半是装插件之前打开的）。把它关掉再点「启动 Max」；或在 Max 里运行 启动白底执行器.ms。`+hiddenNote
        :offline&&!status?.can_start?'找不到可启动的 Max。'+hiddenNote
        :offline?'Max 没有打开。点「启动 Max」会自动打开并连上。'+hiddenNote:'';
      start.hidden=starting||!status?.can_start;
      copy.hidden=starting||!status?.max_running;
    }
  }
  function update(value){
    status=typeof value?.online==='boolean'?value:null;
    if(status?.online){clearTimeout(deadline);starting=false;}
    render();
  }
  function readHome(){
    if(!homeRequest)homeRequest=api('/api/runner').then(
      data=>{if(!document.getElementById('start-page').hidden)update(data.runner??data);},
      ()=>{if(!document.getElementById('start-page').hidden)update(null);}
    ).finally(()=>homeRequest=null);
    return homeRequest;
  }
  setInterval(()=>{if(!document.getElementById('start-page').hidden)readHome();},5000);
  render();
  return {update,readHome,queued(){
    if(status?.online!==false)return false;
    const chip=chips.find(({host})=>!host.closest('section').hidden)?.chip;
    chip?.animate([{boxShadow:'0 0 0 3px #f8717199'},{boxShadow:'0 0 0 0 transparent'}],650);
    toast('Max 未连接，任务会在 Max 启动后开始');
    return true;
  }};
}
