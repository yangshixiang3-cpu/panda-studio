import {syncRange} from './range.js';
import {uploadBlob} from './api.js';

export const DEFAULT_LIGHTING={enabled:false,azimuth_deg:null,elevation_deg:35,intensity:1,contrast:.5};
export const DEFAULT_LIGHTING_TUNING=Object.freeze({
  fill_min:.10,fill_gamma:1,env_gi_min:.15,key_size_max:.50,key_size_min:.25,
  key_distance:2.5,key_intensity:1,key_compensation:111,bg_fill_keep:0,ai_mul:1,
});
const finite=value=>typeof value==='number'&&Number.isFinite(value);
export function previewLightingSettings(task){
  const tuning=task?.lighting_tuning,settings={...DEFAULT_LIGHTING_TUNING};
  for(const key of ['fill_min','env_gi_min','bg_fill_keep']){
    const value=tuning?.[key];if(finite(value)&&value>=0&&value<=1)settings[key]=value;
  }
  for(const key of ['fill_gamma','key_size_max','key_size_min','key_distance']){
    const value=tuning?.[key];if(finite(value)&&value>0)settings[key]=value;
  }
  if(settings.key_size_min>settings.key_size_max){
    settings.key_size_min=DEFAULT_LIGHTING_TUNING.key_size_min;
    settings.key_size_max=DEFAULT_LIGHTING_TUNING.key_size_max;
  }
  const key=tuning?.key_intensity,base=task?.key_light_intensity,compensation=tuning?.key_compensation;
  if(finite(base)&&base>0)settings.key_intensity=base;
  if(finite(key)&&key>0)settings.key_intensity=key;
  if(finite(compensation)&&compensation>=0)settings.key_compensation=compensation;
  const aiMul=task?.ai_calibration?.accepted_overrides?.lights?.intensity_mul;
  if(finite(aiMul))settings.ai_mul=Math.max(.85,Math.min(1.15,aiMul));
  return settings;
}
export function lightingResponse(light,tuning=DEFAULT_LIGHTING_TUNING){
  const {contrast,intensity}=normalizeLighting(light);
  const fill=1-(1-tuning.fill_min)*contrast**tuning.fill_gamma,loss=1-fill;
  return {
    fill_ratio:fill,bg_fill_ratio:fill+loss*tuning.bg_fill_keep,
    env_gi:1-(1-tuning.env_gi_min)*contrast,
    key_size:contrast===1?tuning.key_size_min:tuning.key_size_max-(tuning.key_size_max-tuning.key_size_min)*contrast,
    key_distance:tuning.key_distance,
    key_strength:tuning.key_intensity*intensity*tuning.ai_mul*(1+tuning.key_compensation*loss),
    fill_scale:intensity*tuning.ai_mul,
  };
}
export function previewKeyIrradiance(response){
  // Corona 面光的尺寸和距离决定产品收到的照度，方向灯需单独换算。
  // Max 收近主光时按距离平方减弱功率，预览使用名义距离可保留同一照度。
  const relativeAngularSize=(response.key_size/response.key_distance)/(.5/2.5);
  return 3*(response.key_strength/20)*relativeAngularSize**2;
}
export function previewFinishTint(name){
  // FBX 有时只保留 Corona 面材名称；明确标记的黑色饰面可近似还原。
  return /纯黑|哑黑|(?:^|[ _-])matte[ _-]+black/i.test(name||'')?0x606060:null;
}
export function normalizeLighting(value){
  value=value||{};const result={...DEFAULT_LIGHTING};
  result.enabled=value.enabled===true;
  result.azimuth_deg=finite(value.azimuth_deg)?Math.max(-180,Math.min(180,value.azimuth_deg)):null;
  for(const [key,low,high] of [['elevation_deg',5,85],['intensity',.6,1.5],['contrast',0,1]])if(finite(value[key]))result[key]=Math.max(low,Math.min(high,value[key]));
  return result;
}
export const mergedLighting=(task,item)=>normalizeLighting(item?.lighting??task?.lighting);
export const lightingActive=light=>light.enabled===true&&finite(light.azimuth_deg);
export const effectiveLighting=(task,item)=>{
  const light=mergedLighting(task,item);
  return lightingActive(light)?light:DEFAULT_LIGHTING;
};
export function lightingText(value){
  const light=normalizeLighting(value);if(!lightingActive(light))return '模板默认灯光';
  const {azimuth_deg:az,elevation_deg:el,contrast}=light;
  const side=az<0?'左':'右',horizontal=Math.abs(az)<20?'正前方':Math.abs(az)<70?side+'前方':Math.abs(az)<=110?side+'侧':side+'后方（逆光）';
  const height=el<20?'低角度':el<=55?'斜上方':'高处（接近顶光）';
  let faces=Math.abs(az)<20?'正面受光、两侧略暗':az<0?'左侧受光较亮、右侧偏暗':'右侧受光较亮、左侧偏暗';if(el>55)faces+='、顶面最亮';
  const strength=contrast<.3?'柔和':contrast<=.7?'中等':'强烈',number=n=>{
    const scaled=Math.floor(Math.abs(n)*1000000+.5),whole=Math.floor(scaled/1000000),fraction=scaled%1000000;
    return (n<0&&scaled?'−':'')+whole+(fraction?'.'+String(fraction).padStart(6,'0').replace(/0+$/,''):'');
  };
  return `主光从${horizontal}${height}约 ${Math.floor(el/5+.5)*5}° 照来（水平 ${number(az)}°，高 ${number(el)}°）；${faces}；明暗对比${strength}。`;
}
// 屏幕向右为 +X，向下为正面 +Z，正面在圆盘下方。
export function diskAzimuth(x,y,cx=90,cy=90){return Math.atan2(x-cx,y-cy)*180/Math.PI;}
export function diskPoint(az,radius=72){const a=az*Math.PI/180;return [90+radius*Math.sin(a),90+radius*Math.cos(a)];}
export const lightDirection=light=>{const a=light.azimuth_deg*Math.PI/180,e=light.elevation_deg*Math.PI/180;return [Math.sin(a)*Math.cos(e),Math.sin(e),Math.cos(a)*Math.cos(e)];};

export function installLightingUI({$,getTask,getItem,getViewer,requestDraw,lightChanged,busy}){
  const presets=[['左前光',-45,35],['右前光',45,35],['正面光',0,30],['顶光',-20,70]];
  $('lighting-presets').innerHTML=presets.map(([label],i)=>`<button data-light-preset="${i}">${label}</button>`).join('');
  let queue=Promise.resolve(),drag=null,uploadedKey,previewSettingsKey;
  const settingsKey=task=>JSON.stringify(previewLightingSettings(task));
  // 任务轮询或切回前台时按需核对灯光调参快照，不再常驻 500ms 空转定时器。
  function notifyTaskChanged(){
    if(typeof document!=='undefined'&&document.hidden)return;
    const task=getTask();
    if(!task||!getViewer())return;
    const key=settingsKey(task);
    if(key!==previewSettingsKey){previewSettingsKey=key;requestDraw();}
  }
  if(typeof document!=='undefined'){
    document.addEventListener('visibilitychange',()=>{if(!document.hidden)notifyTaskChanged();});
  }
  function editable(){const task=getTask(),item=getItem();return item.lighting??(task.lighting??={...DEFAULT_LIGHTING});}
  function edit(apply){
    const task=getTask();if(!task||busy())return;
    lightChanged(apply);
  }
  function disk(azimuth){
    const [x,y]=diskPoint(azimuth??0);
    for(const id of ['lighting-sun','lighting-sun-halo']){
      $(id).setAttribute('cx',x);$(id).setAttribute('cy',y);
    }
    const fill=$('lighting-beam-fill');fill.setAttribute('x1',x);fill.setAttribute('y1',y);
    const nx=(y-90)/72,ny=(90-x)/72;
    $('lighting-beam').setAttribute('d',`M ${x+nx*4} ${y+ny*4} L ${90+nx*25} ${90+ny*25} L ${90-nx*25} ${90-ny*25} L ${x-nx*4} ${y-ny*4} Z`);
  }
  function sync(){
    const task=getTask(),item=getItem();if(!task||!item)return;
    const light=mergedLighting(task,item);
    const settings=previewLightingSettings(task);
    previewSettingsKey=JSON.stringify(settings);
    getViewer()?.setLighting(light,lightingActive(light)&&$('lighting-clay').checked,settings);
    if($('lighting-panel').hidden)return;
    document.querySelectorAll('#lighting-panel input:not(#lighting-clay),#lighting-panel button').forEach(el=>el.disabled=busy());
    $('lighting-disk').setAttribute('aria-disabled',String(busy()));
    $('lighting-disk').setAttribute('aria-valuemin','-180');$('lighting-disk').setAttribute('aria-valuemax','180');$('lighting-disk').setAttribute('aria-valuenow',light.azimuth_deg??0);
    $('lighting-single').checked=item.lighting!==null&&item.lighting!==undefined;
    $('lighting-enabled').checked=light.enabled;
    $('lighting-controls').hidden=!light.enabled;
    if(!light.enabled)return;
    $('lighting-description').textContent=lightingText(light);disk(light.azimuth_deg);
    for(const key of ['elevation_deg','intensity','contrast']){
      syncRange($('light-'+key),light[key]);$('light-value-'+key).textContent=key==='elevation_deg'?`${light[key]}°`:light[key].toFixed(2);
    }
    const size=getViewer()?.model?.size||[1200,850,400],scale=70/Math.max(size[0],size[2]);
    const outline=$('lighting-product');outline.setAttribute('x',90-size[0]*scale/2);outline.setAttribute('y',90-size[2]*scale/2);outline.setAttribute('width',size[0]*scale);outline.setAttribute('height',size[2]*scale);
    $('lighting-disk').querySelector('text').setAttribute('y',90+size[2]*scale/2+16);
    document.querySelectorAll('[data-light-preset]').forEach((b,i)=>b.classList.toggle('active',light.azimuth_deg===presets[i][1]&&light.elevation_deg===presets[i][2]));
  }
  for(const key of ['elevation_deg','intensity','contrast'])$('light-'+key).oninput=()=>edit(()=>editable()[key]=Number($('light-'+key).value));
  $('lighting-presets').onclick=e=>{
    const b=e.target.closest('[data-light-preset]');if(!b)return;
    const [,azimuth_deg,elevation_deg]=presets[Number(b.dataset.lightPreset)];
    edit(()=>Object.assign(editable(),{azimuth_deg,elevation_deg}));
  };
  $('lighting-single').onchange=()=>edit(()=>{const item=getItem();item.lighting=$('lighting-single').checked?mergedLighting(getTask(),item):null;});
  $('lighting-enabled').onchange=()=>edit(()=>{
    const light=editable(),enabled=$('lighting-enabled').checked;
    if(enabled&&light.azimuth_deg==null)Object.assign(light,DEFAULT_LIGHTING,{azimuth_deg:-45});
    light.enabled=enabled;
  });
  $('lighting-clay').onchange=requestDraw;
  function move(e){
    const r=drag.rect;
    edit(()=>editable().azimuth_deg=Math.round(diskAzimuth((e.clientX-r.left)*180/r.width,(e.clientY-r.top)*180/r.height)));
  }
  $('lighting-disk').onpointerdown=e=>{
    if(busy()||drag||e.button!==0)return;
    drag={id:e.pointerId,rect:$('lighting-disk').getBoundingClientRect()};
    $('lighting-disk').setPointerCapture(e.pointerId);move(e);
  };
  $('lighting-disk').onpointermove=e=>{if(drag?.id===e.pointerId)move(e);};
  $('lighting-disk').onpointerup=$('lighting-disk').onpointercancel=$('lighting-disk').onlostpointercapture=e=>{if(drag?.id===e.pointerId)drag=null;};
  $('lighting-disk').onkeydown=e=>{
    if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key))return;
    e.preventDefault();const delta=e.key==='ArrowLeft'||e.key==='ArrowDown'?-5:5;
    edit(()=>{const light=editable();light.azimuth_deg=Math.max(-180,Math.min(180,(light.azimuth_deg??0)+delta));});
  };
  async function upload(){
    const current=getTask(),viewer=getViewer();if(!current)return;
    const task=structuredClone(current);
    const enabled=task.items.filter(i=>i.enabled&&lightingActive(mergedLighting(task,i)));
    if(!enabled.length)return;
    if(!viewer?.model)throw new Error('请等预览模型加载完成后，再生成光向示意图。');
    const id=task.id,model=viewer.model,settings=previewLightingSettings(task);
    const key=JSON.stringify([id,task.preview_fbx,task.preview_rotation_deg,settings,enabled.map(i=>[i.no,i.camera,i.orientation,mergedLighting(task,i)])]);
    if(key===uploadedKey)return;
    // 逐张生成，清单较长时也只保留一张大画布；每张都使用本次操作的设置快照。
    for(const item of enabled){
      if(getTask()?.id!==id||viewer.model!==model)return;
      const canvas=viewer.lightingImage(item,mergedLighting(task,item),settings);
      const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));if(!blob)throw new Error('光向示意图生成失败。');
      await uploadBlob(`/api/tasks/${id}/lighting-images?no=${item.no}`,blob,{headers:{'Content-Type':'image/png'},fallback:'光向示意图保存失败'});
    }
    uploadedKey=key;
  }
  function images(){queue=queue.catch(()=>{}).then(upload);return queue;}
  return {sync,images,notifyTaskChanged};
}

