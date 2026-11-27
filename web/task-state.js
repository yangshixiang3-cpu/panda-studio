const runningStates=['queued','rendering','running','waiting_lock','pending'];
const running=state=>runningStates.includes(state);
export const renderModes=['smallshot','final'];
export const hasRunningItems=task=>task.items.some(item=>renderModes.some(mode=>running(item[mode].state)));

export function rowStatus(item){
  const {smallshot,final}=item;
  const mode=running(smallshot.state)&&!running(final.state)||final.state==='none'?'smallshot':'final';
  const {state,message}=item[mode];
  const labels=mode==='final'?{queued:'渲染排队',rendering:'渲染中',done:'成品 ✓'}
    :{queued:'小样排队',rendering:'出小样中',done:'小样 ✓'};
  return {state,text:state==='failed'?'失败':labels[state]||'未出图',message};
}

function deliveredItems(task,enabled,jobs){
  if(!task.delivered)return false;
  const {at,files}=task.delivered;
  return enabled.every(item=>{
    const job=jobs?.find(j=>j.job_id===item.final.job_id);
    const ended=job?.finished_at??job?.updated_at??job?.started_at;
    const name=item.final.file?.replaceAll('\\','/').split('/').at(-1);
    return (!ended||!at||ended<=at)&&(!files||files.includes(name));
  });
}

function etaText(seconds){
  if(!Number.isFinite(seconds)||seconds<=0)return '';
  return seconds<60?` · 约 ${Math.max(1,Math.round(seconds))} 秒`:` · 约 ${Math.round(seconds/60)} 分钟`;
}

export function taskSteps(task,bundle){
  const enabled=task.items.filter(item=>item.enabled);
  const shotDone=enabled.length>0&&enabled.every(item=>item.smallshot.state==='done');
  const finalDone=enabled.length>0&&enabled.every(item=>item.final.state==='done');
  const delivered=finalDone&&deliveredItems(task,enabled,bundle?.jobs);
  const current=delivered||!enabled.length?'':!shotDone?'smallshot':!finalDone?'final':'delivery';
  const progress=bundle?.progress;
  return [['smallshot','出小样',shotDone||delivered],['final','渲染',finalDone],['delivery','入库',delivered]].map(([mode,label,done])=>{
    const matchesProgress=mode!=='delivery'&&progress?.kind===mode;
    const active=mode!=='delivery'&&(matchesProgress||enabled.some(item=>running(item[mode].state)));
    let text=delivered&&mode==='delivery'?'已入库 ✓':label+(done?' ✓':'');
    if(active){
      text=label+(matchesProgress&&Number.isFinite(progress.done)&&Number.isFinite(progress.total)
        ?` ${progress.done}/${progress.total}${etaText(progress.eta_sec)}`:' 进行中');
    }
    return {mode,text,current:mode===current,active};
  });
}

export function pollDelay(bundle,calibrating){
  const active=bundle?.progress?bundle.progress.kind:calibrating||bundle?.jobs?.some(job=>running(job.state));
  return active||bundle?.runner?.online===false?1800:8000;
}

export function bundleSignature(data){
  if(data.signature!=null)return 'signature:'+data.signature;
  const revision=data.revision??data.task.revision;
  return revision!=null?'revision:'+revision:JSON.stringify(data);
}

export function renumberResult(result,oldNo,newNo,folder){
  if(!result)return result;
  const next={...result};
  if(oldNo===newNo)return next;
  for(const [key,suffix] of [['file','.jpg'],['mask','_遮罩_ID.jpg']]){
    if(String(next[key]||'').replaceAll('\\','/')===`${folder}/${oldNo}${suffix}`){
      next[key]=`${folder}/${newNo}${suffix}`;
    }
  }
  if(next.job_id&&next.job_item?.job_id!==next.job_id){
    next.job_item={job_id:next.job_id,no:oldNo};
  }
  return next;
}

export function renumberItems(items){
  return items.map((item,index)=>{
    const oldNo=item.no,no=index+1;
    return {...item,no,
      smallshot:renumberResult(item.smallshot,oldNo,no,'smallshots'),
      final:renumberResult(item.final,oldNo,no,'final'),
    };
  });
}

export function shouldPauseBackground(page, hidden=false){
  return Boolean(hidden||page==='task'||page==='canvas');
}

export function syncBackgroundPause(doc, page){
  if(!doc?.body)return false;
  const hidden=Boolean(doc.hidden);
  const paused=shouldPauseBackground(page,hidden);
  if(doc.body.classList.contains('bg-paused')!==paused)doc.body.classList.toggle('bg-paused',paused);
  if(doc.body.classList.contains('page-hidden')!==hidden)doc.body.classList.toggle('page-hidden',hidden);
  return paused;
}
