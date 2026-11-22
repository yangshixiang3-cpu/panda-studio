// 渲染位置（本机 Max / 内网渲染机）的显示文字。纯函数，网页和 tests/web 共用。

const connecting=target=>!target.reachable&&String(target.error||'').includes('正在连接');

// 顶部状态片上的文字
export function chipText(status,starting=false){
  if(starting)return '正在启动 Max…';
  const target=status?.target;
  if(target){
    const name=target.name||target.host;
    if(connecting(target))return `渲染机 ${name} · 连接中`;
    if(!target.reachable)return `渲染机 ${name} · 连不上`;
    if(!target.paired)return `渲染机 ${name} · 要连接码`;
    if(status.online)return status.busy?`渲染机 ${name} · 忙`:`渲染机 ${name}`;
    return `渲染机 ${name} · Max 未打开`;
  }
  if(status?.online===true)return 'Max';
  if(status?.online===false)return status.max_running?'Max 已开 · 未连接':'Max 未连接';
  return 'Max 状态未知';
}

// 状态片的颜色：online 绿、busy 琥珀、offline 红、unknown 灰
export function chipState(status){
  if(!status||typeof status.online!=='boolean')return 'unknown';
  if(status.target&&connecting(status.target))return 'unknown';
  if(status.online)return status.busy?'busy':'online';
  return 'offline';
}

// 渲染机列表每一行右侧的小标签：{text, tone}，tone 为 ok / warn / bad / muted
export function nodeBadge(node){
  if(!node.reachable)return {text:'连不上',tone:'bad'};
  if(!node.paired)return {text:'要连接码',tone:'muted'};
  if(node.template===false)return {text:'缺白底模板',tone:'bad'};
  if(!node.online)return {text:node.max_running?'Max 已开 · 未连接':'Max 未打开',tone:'muted'};
  if(node.busy||node.queue>0)return {text:node.queue>0?`忙 · ${node.queue} 个任务`:'忙',tone:'warn'};
  return {text:'空闲',tone:'ok'};
}

export const validCode=value=>/^\d{6}$/.test(String(value??'').trim());
export const nodeKey=node=>`${node.host}:${node.port}`;
