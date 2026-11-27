// 顶部「渲染位置」：显示本机 Max 或所选内网渲染机的状态；点开可以换成别的渲染机（自动发现 + 手动添加 + 连接码配对）。
import {chipText,chipState,nodeBadge,nodeKey,validCode} from './render-target.js';

const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const CHECK='<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>';

export function installRunner({api,toast,refreshTask}){
  let status=null,starting=false,deadline,homeRequest,signature;
  let choices={target:null,nodes:[]},scanning=false,scannedAt=0,pairing='',busy=false;
  const chips=[...document.querySelectorAll('[data-runner]')].map(host=>{
    host.className='runner-status';
    host.innerHTML=`<button class="runner-chip" type="button" aria-expanded="false" aria-haspopup="dialog"></button>
      <div class="popover runner-panel" role="dialog" aria-label="渲染位置" hidden>
        <div class="rt-head"><strong>渲染位置</strong><button class="rt-scan plain" type="button">查找渲染机</button></div>
        <div class="rt-list" role="radiogroup" aria-label="渲染位置"></div>
        <div class="rt-local-help"><p class="runner-message"></p><div class="rt-actions"><button class="primary runner-start" type="button" hidden>启动 Max</button><button class="runner-copy" type="button" hidden>复制脚本路径</button></div></div>
        <form class="rt-add"><input type="text" spellcheck="false" autocomplete="off" placeholder="找不到？输入渲染机的电脑名或 IP" aria-label="渲染机电脑名或 IP"><button type="submit">添加</button></form>
      </div>`;
    const q=s=>host.querySelector(s);
    const ui={host,chip:q('.runner-chip'),panel:q('.runner-panel'),list:q('.rt-list'),scan:q('.rt-scan'),
      help:q('.rt-local-help'),message:q('.runner-message'),start:q('.runner-start'),copy:q('.runner-copy'),add:q('.rt-add')};
    ui.open=value=>{
      ui.panel.hidden=!value;ui.chip.setAttribute('aria-expanded',String(value));
      if(value){renderPanels();if(Date.now()-scannedAt>30000)scan();}
    };
    ui.chip.onclick=()=>ui.open(ui.panel.hidden);
    ui.scan.onclick=()=>scan(true);
    ui.start.onclick=async()=>{
      if(starting)return;
      starting=true;render();
      deadline=setTimeout(()=>{starting=false;render();toast('Max 启动较慢或启动失败，请查看 Max 窗口');},180000);
      try{await api('/api/runner/start',{});}
      catch(error){clearTimeout(deadline);starting=false;render();toast(error.message);return;}
      refreshCurrent();
    };
    ui.copy.onclick=async()=>{
      try{
        const {root}=await api('/api/health');
        if(!root)throw new Error('无法获取执行器脚本路径。');
        await navigator.clipboard.writeText(root.replace(/[\\/]+$/,'').replaceAll('/','\\')+'\\max\\启动白底执行器.ms');
        toast('已复制脚本路径');
      }catch(error){toast(error.message);}
    };
    ui.add.onsubmit=async event=>{
      event.preventDefault();
      const input=ui.add.querySelector('input'),address=input.value.trim();
      if(!address)return input.focus();
      await act(async()=>{
        const node=await api('/api/render-target/add',{address});
        input.value='';
        await scan(true,false);
        pairing=nodeKey(node);renderPanels();focusPairing(ui);
      });
    };
    ui.list.addEventListener('click',event=>{
      const row=event.target.closest('.rt-row');
      if(!row||event.target.closest('.rt-pair'))return;
      choose(row.dataset.key,ui);
    });
    ui.list.addEventListener('keydown',event=>{
      const row=event.target.closest('.rt-row');
      if(row&&(event.key==='Enter'||event.key===' ')&&!event.target.closest('.rt-pair')){event.preventDefault();choose(row.dataset.key,ui);}
    });
    ui.list.addEventListener('submit',async event=>{
      event.preventDefault();
      const form=event.target,input=form.querySelector('input'),code=input.value.trim();
      const node=nodes().find(n=>nodeKey(n)===form.dataset.key);
      if(!node)return;
      if(!validCode(code)){input.focus();return toast('连接码是渲染机设置页里显示的 6 位数字');}
      await act(async()=>{
        const result=await api('/api/render-target/pair',{host:node.host,port:node.port,code});
        pairing='';update(result.runner);await scan(false,false);
        toast(`已连接渲染机 ${node.name}，之后出图都交给它`);refreshCurrent();
      });
    });
    // 用事件路径判断：点了面板里的一行后列表会重绘，event.target 已不在页面上，但路径里仍有 host
    document.addEventListener('click',event=>{if(!event.composedPath().includes(host)&&!ui.panel.hidden)ui.open(false);});
    host.addEventListener('keydown',event=>{if(event.key==='Escape'&&!ui.panel.hidden){ui.open(false);ui.chip.focus();}});
    return ui;
  });

  async function act(work){
    if(busy)return;
    busy=true;renderPanels();
    try{await work();}catch(error){toast(error.message);}
    finally{busy=false;renderPanels();}
  }
  function refreshCurrent(){
    const onTask=!document.getElementById('task-page')?.hidden;
    (onTask?refreshTask():readHome()).catch(error=>toast(error.message));
  }
  function focusPairing(ui){requestAnimationFrame(()=>ui.list.querySelector('.rt-pair input')?.focus());}
  // 列表里的渲染机：扫描结果；当前选中的渲染机即使没扫到也列出来
  // 当前选中的那台用实时状态（顶部状态片同一份），不用上次扫描的旧信息
  function nodes(){
    const target=status?.target;
    const live=target&&{host:target.host,port:target.port,name:target.name,reachable:target.reachable,paired:target.paired,
      online:status.online,busy:status.busy,max_running:status.max_running,queue:target.queue,template:target.template};
    const list=choices.nodes.map(n=>live&&nodeKey(n)===nodeKey(live)?{...n,...live}:n);
    if(live&&!list.some(n=>nodeKey(n)===nodeKey(live)))list.unshift(live);
    return list;
  }
  async function scan(force=false,show=true){
    if(scanning&&!force)return;
    scanning=true;if(show)renderPanels();
    try{choices=await api('/api/render-target/scan',{});scannedAt=Date.now();}
    catch(error){if(force)toast(error.message);}
    finally{scanning=false;renderPanels();}
  }
  async function choose(key,ui){
    if(busy)return;
    const target=status?.target;
    if(key==='local'){
      if(!target)return;
      return act(async()=>{const result=await api('/api/render-target/select',{mode:'local'});update(result.runner);toast('已换回本机 Max');refreshCurrent();});
    }
    const node=nodes().find(n=>nodeKey(n)===key);
    if(!node)return;
    if(!node.paired){pairing=pairing===key?'':key;renderPanels();return focusPairing(ui);}
    if(target&&nodeKey(target)===key)return;
    await act(async()=>{
      const result=await api('/api/render-target/select',{host:node.host,port:node.port});
      update(result.runner);toast(`已换成渲染机 ${node.name}`);refreshCurrent();
    });
  }

  function rowHtml({key,title,detail,badge,selected,disabled}){
    return `<div class="rt-row${selected?' selected':''}${disabled?' dim':''}" role="radio" tabindex="0" aria-checked="${selected}" data-key="${esc(key)}">
      <span class="rt-radio">${selected?CHECK:''}</span>
      <span class="rt-main"><strong>${esc(title)}</strong><small>${esc(detail)}</small></span>
      ${badge?`<span class="rt-badge ${badge.tone}">${esc(badge.text)}</span>`:''}
      ${pairing===key?`<form class="rt-pair" data-key="${esc(key)}"><input inputmode="numeric" maxlength="6" autocomplete="off" placeholder="6 位连接码" aria-label="连接码"><button class="primary" type="submit">连接</button><span>连接码在渲染机的「本机设置 → 渲染机」里</span></form>`:''}
    </div>`;
  }
  function renderPanels(){
    const target=status?.target;
    const localBadge=target?null:status?.online?{text:'已连接',tone:'ok'}:status?.online===false
      ?{text:status.max_running?'已开 · 未连接':'未连接',tone:'bad'}:null;
    const rows=[rowHtml({key:'local',title:'本机 Max',detail:'这台电脑上的 3ds Max',badge:localBadge,selected:!target})];
    for(const node of nodes()){
      const key=nodeKey(node);
      rows.push(rowHtml({key,title:node.name||node.host,detail:node.host+(node.port!==8775?':'+node.port:''),
        badge:nodeBadge(node),selected:!!target&&nodeKey(target)===key,disabled:!node.reachable}));
    }
    if(!nodes().length)rows.push(`<p class="rt-empty">${scanning?'正在内网里查找渲染机…':'内网里还没找到渲染机。渲染机那边在「本机设置」点「设为渲染机」，并打开 Max。'}</p>`);
    for(const ui of chips){
      const focused=ui.list.contains(document.activeElement)?document.activeElement:null;
      const typed=ui.list.querySelector('.rt-pair input')?.value||'';
      ui.list.innerHTML=rows.join('');
      ui.list.setAttribute('aria-busy',String(busy||scanning));
      const input=ui.list.querySelector('.rt-pair input');
      if(input){input.value=typed;if(focused?.matches('.rt-pair input'))input.focus();}
      ui.scan.textContent=scanning?'正在查找…':'查找渲染机';ui.scan.disabled=scanning;
    }
  }
  function render(){
    const current=JSON.stringify([status,starting]);
    if(signature===current)return;
    signature=current;
    const target=status?.target;
    const title=(status?.max_windows||[])[0]||'';
    const hiddenNote=status?.max_hidden?`（后台另有 ${status.max_hidden} 个隐藏的 Max，是其他自动化在用，不影响。）`:'';
    const offline=status?.online===false;
    const message=target
      ?(target.reachable&&target.paired&&!status.online?`渲染机 ${target.name} 上的 Max 没打开（或没加载白底执行器）。任务会先在那边排队，Max 打开后自动开始。`
        :target.reachable&&!target.paired?'这台渲染机还没输入连接码：点上面的渲染机，输入它设置页里的 6 位连接码。'
        :!target.reachable&&!String(target.error||'').includes('正在连接')?`连不上渲染机 ${target.name}：确认那台电脑开着、连着公司内网。任务会等它恢复后自动发送。`:'')
      :starting?'正在启动 Max…'
      :status?.max_running?`开着的 Max「${title.replace(/\s*-\s*Autodesk 3ds Max.*$/,'')||'3ds Max'}」没有加载白底执行器（多半是装插件之前打开的）。把它关掉再点「启动 Max」；或在 Max 里运行 启动白底执行器.ms。`+hiddenNote
      :offline&&!status?.can_start?'找不到可启动的 Max。'+hiddenNote
      :offline?'Max 没有打开。点「启动 Max」会自动打开并连上。'+hiddenNote:'';
    for(const ui of chips){
      ui.host.dataset.state=chipState(status);
      ui.host.dataset.target=target?'remote':'local';
      ui.chip.textContent=chipText(status,starting);
      ui.chip.title=target?`正在用内网渲染机 ${target.name}（${target.host}）出图，点这里换`:'点这里选择用哪台电脑的 Max 出图';
      ui.message.textContent=message;
      ui.help.hidden=!message;
      ui.start.hidden=!!target||starting||!status?.can_start;
      ui.copy.hidden=!!target||starting||!status?.max_running;
    }
    renderPanels();
  }
  function update(value){
    status=typeof value?.online==='boolean'?value:null;
    if(status?.online){clearTimeout(deadline);starting=false;}
    render();
  }
  let homeTimer=null,homeActive=false;
  const HOME_POLL_MS=5000;
  const isHomeVisible=()=>homeActive&&!document.hidden&&!document.getElementById('start-page')?.hidden;
  function stopHomePoll(){
    if(homeTimer){clearTimeout(homeTimer);homeTimer=null;}
  }
  function scheduleHomePoll(){
    stopHomePoll();
    if(!isHomeVisible())return;
    homeTimer=setTimeout(()=>{homeTimer=null;if(isHomeVisible())readHome();},HOME_POLL_MS);
  }
  function readHome(){
    homeActive=!document.getElementById('start-page')?.hidden;
    if(document.hidden){stopHomePoll();return Promise.resolve();}
    if(!homeRequest)homeRequest=api('/api/runner').then(
      data=>{if(!document.getElementById('start-page').hidden)update(data.runner??data);},
      ()=>{if(!document.getElementById('start-page').hidden)update(null);}
    ).finally(()=>{homeRequest=null;scheduleHomePoll();});
    return homeRequest;
  }
  function setHomeActive(active){
    homeActive=Boolean(active);
    if(!homeActive||document.hidden){stopHomePoll();return;}
    readHome();
  }
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden)stopHomePoll();
    else if(isHomeVisible())readHome();
  });
  render();
  return {update,readHome,setHomeActive,queued(){
    if(status?.online!==false)return false;
    const chip=chips.find(({host})=>!host.closest('section').hidden)?.chip;
    chip?.animate([{boxShadow:'0 0 0 3px #f8717199'},{boxShadow:'0 0 0 0 transparent'}],650);
    const target=status?.target;
    toast(!target?'Max 未连接，任务会在 Max 启动后开始'
      :!target.reachable?`渲染机 ${target.name} 连不上，任务会等它恢复后自动发送`
      :!target.paired?`渲染机 ${target.name} 还没输入连接码，点顶部的渲染位置输入`
      :`渲染机 ${target.name} 的 Max 没打开，任务已送过去排队，打开后自动开始`);
    return true;
  }};
}
