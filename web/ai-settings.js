const structuredLabels={json_schema:'JSON Schema',json_object:'JSON 对象',text:'文本 JSON'};

export function installAISettings({$,api,toast}){
  const dialog=$('ai-settings-dialog'),form=$('ai-settings-form'),fields=$('ai-relay-fields');
  const address=$('ai-base-url'),key=$('ai-key'),model=$('ai-model'),effort=$('ai-effort');
  const clear=$('ai-clear-key'),models=$('ai-models'),count=$('ai-model-count');
  const status=$('ai-settings-status'),result=$('ai-test-result');
  const load=$('ai-load-models'),test=$('ai-test-connection'),save=$('ai-settings-save');
  const cancel=$('ai-settings-cancel');
  let saved,clearKey=false,pending='',session=0;

  function description(){
    if(!saved)return '正在读取引擎…';
    const relay=saved.engine==='openai_api';
    return (relay?'中转站':'本机 Codex')+' · '+((relay?saved.model:saved.codex_model)||'未选择模型');
  }
  function showEngine(){
    const label=$('calibration-engine');
    if(label)label.textContent=description();
  }
  async function refresh(){
    saved=await api('/api/ai/settings');
    showEngine();
    return saved;
  }
  function showStatus(message='',error=false){
    status.hidden=!message;
    status.textContent=message;
    status.className=error?'error':'hint';
  }
  const engine=()=>form.querySelector('input[name="ai-engine"]:checked').value;
  const formFields=()=>({engine:engine(),base_url:address.value,model:model.value,reasoning_effort:effort.value||null});
  function sync(){
    fields.hidden=engine()!=='openai_api';
    $('ai-codex-note').hidden=engine()!=='codex';
    // 换了引擎，上一次的测试结果不再对应，清掉
    if(result.dataset.engine&&result.dataset.engine!==engine()){result.hidden=true;result.replaceChildren();delete result.dataset.engine;}
    key.placeholder=clearKey?'保存后清除':saved?.key_set?'已保存'+(saved.key_tail?' ····'+saved.key_tail:''):'输入 API Key';
    clear.textContent=clearKey?'撤销清除':'清除';
    clear.disabled=!!pending||!saved?.key_set;
    save.disabled=!!pending||!saved;
    load.disabled=test.disabled=!!pending;
    cancel.disabled=pending==='正在保存…';
    for(const input of form.querySelectorAll('input,select'))input.disabled=!!pending;
  }
  function resetResults(){
    models.replaceChildren();
    count.hidden=true;
    result.hidden=true;
    result.replaceChildren();
  }
  async function run(message,work){
    if(pending)return;
    const current=session,active=()=>dialog.open&&session===current;
    pending=message;
    showStatus(message);
    sync();
    try{
      const done=await work();
      if(active()){
        showStatus();
        done?.();
      }
    }catch(error){
      if(active())showStatus(error.status?error.message:'无法连接工作台服务',true);
    }finally{
      if(session===current){pending='';sync();}
    }
  }
  function open(){
    if(dialog.open)return;
    ++session;
    pending='';
    clearKey=false;
    key.value='';
    resetResults();
    dialog.showModal();
    return run('正在读取设置…',async()=>{
      const data=await refresh();
      return ()=>{
        form.querySelector(`input[name="ai-engine"][value="${data.engine==='openai_api'?'openai_api':'codex'}"]`).checked=true;
        address.value=data.base_url;
        model.value=data.model;
        effort.value=data.reasoning_effort||'';
      };
    });
  }
  function canConnect(needsModel){
    if(!address.value||!address.reportValidity()){showStatus('请填写有效的接口地址',true);return false;}
    if(key.value.trim()||clearKey){showStatus('请先保存 API Key 的更改',true);return false;}
    if(!saved?.key_set){showStatus('请先填写并保存 API Key',true);return false;}
    if(needsModel&&!model.value.trim()){showStatus('请选择或填写模型',true);return false;}
    return true;
  }
  function testLine(label,passed){
    const line=document.createElement('li');
    line.className=passed?'passed':'failed';
    line.textContent=(passed?'✓ ':'✕ ')+label;
    result.append(line);
  }
  load.onclick=()=>{
    if(!canConnect(false))return;
    return run('正在拉取模型…',async()=>{
      const data=await api('/api/ai/models',formFields());
      return ()=>{
        models.replaceChildren(...data.models.map(name=>Object.assign(document.createElement('option'),{value:name})));
        if(!model.value&&data.models.length)model.value=data.models[0];
        count.textContent=data.models.length?'已拉取 '+data.models.length+' 个模型':'中转站没有返回模型';
        count.hidden=false;
        result.hidden=true;
      };
    });
  };
  test.onclick=()=>{
    // 本机 Codex：不需要接口地址和 Key，检查命令、登录和校准结构文件（不调用模型，不花额度）
    if(engine()==='codex'){
      result.hidden=true;
      return run('正在检查本机 Codex…',async()=>{
        const data=await api('/api/ai/test',{engine:'codex'});
        return ()=>{
          result.replaceChildren();
          testLine('找到 Codex 命令'+(data.version?' · '+data.version:''),data.connected);
          testLine('已登录'+(data.logged_in&&data.account?' · '+data.account:''),data.logged_in);
          testLine('校准结构文件',data.schema);
          if(data.connected&&data.logged_in&&data.schema)testLine('可以用 '+data.model+' 校准',true);
          result.dataset.engine='codex';result.hidden=false;
          if(data.reason)showStatus(data.reason,true);
        };
      });
    }
    if(!canConnect(true))return;
    result.hidden=true;
    return run('正在测试连接…',async()=>{
      const data=await api('/api/ai/test',formFields());
      return ()=>{
        result.replaceChildren();
        testLine('连接',data.connected);
        testLine('图片识别',data.vision);
        testLine('输出格式'+(data.structured_mode?' · '+structuredLabels[data.structured_mode]:''),!!data.structured_mode);
        if(data.connected)testLine(data.model+' · '+data.latency_sec+' 秒',true);
        result.dataset.engine='openai_api';result.hidden=false;
        if(data.reason)showStatus(data.reason,true);
      };
    });
  };
  for(const input of form.querySelectorAll('input[name="ai-engine"]'))input.onchange=sync;
  address.oninput=()=>{resetResults();showStatus();};
  model.oninput=effort.onchange=()=>{result.hidden=true;showStatus();};
  key.oninput=()=>{if(key.value)clearKey=false;resetResults();showStatus();sync();};
  clear.onclick=()=>{key.value='';clearKey=!clearKey;resetResults();showStatus();sync();};
  cancel.onclick=()=>dialog.close();
  dialog.addEventListener('cancel',event=>{if(cancel.disabled)event.preventDefault();});
  dialog.addEventListener('close',()=>{key.value='';++session;});
  form.onsubmit=event=>{
    event.preventDefault();
    const changes=formFields();
    if(!fields.hidden){changes.api_key=key.value;changes.clear_key=clearKey;}
    return run('正在保存…',async()=>{
      const data=await api('/api/ai/settings',changes);
      return ()=>{
        saved=data;
        showEngine();
        dialog.close();
        toast('已保存 AI 设置');
      };
    });
  };
  $('ai-settings-entry').onclick=open;
  return {open,description,showEngine,refresh};
}
