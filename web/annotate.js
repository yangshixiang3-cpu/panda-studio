import {uploadBlob} from './api.js';

export const cropUrlPath=(file,index)=>file.replace(/\/([^/]+)\.jpg$/,'/crops/$1-'+(index+1)+'.jpg');

export function regionStyle(box,material,materials){
  const [x,y,w,h]=box.map(value=>value*100+'%');
  const hue=(Math.max(0,materials.indexOf(material))*137+188)%360;
  return `left:${x};top:${y};width:${w};height:${h};--region-color:hsl(${hue} 88% 68%)`;
}

const clamp=(value,min,max)=>Math.max(min,Math.min(max,value));
const corners={nw:[0,0],ne:[1,0],sw:[0,1],se:[1,1]};
function boxBetween(a,b){
  return [Math.min(a.x,b.x),Math.min(a.y,b.y),Math.abs(a.x-b.x),Math.abs(a.y-b.y)];
}

export function installAnnotator({$,api,escape,fileUrl}){
  const dialog=$('annotate-dialog'),stage=$('annotate-stage'),frame=$('annotate-frame');
  const image=$('annotate-image'),overlay=$('annotate-regions'),select=$('annotate-material');
  const form=$('annotate-material-form'),error=$('annotate-error'),confirm=$('annotate-delete-dialog');
  const picker=$('annotate-task-picker'),pickerToggle=$('annotate-task-toggle'),pickerOptions=$('annotate-task-options');
  let entry,blob,keys,regions,selected,gesture,taskOptions,materialChoices,current;
  let space=false,mode='draw',busy=false;
  let zoom=1,panX=0,panY=0;

  function fit(){
    const scale=Math.min((stage.clientWidth-32)/image.naturalWidth,(stage.clientHeight-32)/image.naturalHeight);
    return {width:image.naturalWidth*scale,height:image.naturalHeight*scale};
  }
  function position(){
    if(!image.naturalWidth||!stage.clientWidth)return;
    const {width,height}=fit();
    panX=clamp(panX,-Math.max(0,(width*zoom-stage.clientWidth)/2+32),Math.max(0,(width*zoom-stage.clientWidth)/2+32));
    panY=clamp(panY,-Math.max(0,(height*zoom-stage.clientHeight)/2+32),Math.max(0,(height*zoom-stage.clientHeight)/2+32));
    frame.style.width=width*zoom+'px';
    frame.style.height=height*zoom+'px';
    frame.style.transform=`translate(-50%, -50%) translate(${panX}px, ${panY}px)`;
    $('annotate-zoom').textContent=Math.round(zoom*100)+'%';
  }
  new ResizeObserver(position).observe(stage);

  function paint(){
    overlay.innerHTML=regions.map(({box,material},index)=>`<div class="annotate-region ${selected===index?'selected':''}" data-region="${index}" style="${regionStyle(box,material,keys)}">
      <span class="region-label">${escape(material)}</span>${selected===index?`<button type="button" class="region-remove" data-remove="${index}" aria-label="删除框">×</button>
      ${Object.keys(corners).map(corner=>`<span class="region-grip" data-corner="${corner}"></span>`).join('')}`:''}</div>`).join('');
  }
  function selectRegion(index){
    selected=index;
    if(index>=0){
      const {material,material_source}=regions[index];
      if(taskOptions)chooseTaskMaterial({material,material_source});
      else select.value=material;
    }
    paint();
  }
  function removeRegion(index){
    regions.splice(index,1);
    selectRegion(-1);
  }
  function options(value){
    select.replaceChildren(...keys.map(key=>new Option(key,key)));
    if(keys.includes(value))select.value=value;
  }
  function closePicker(){
    pickerOptions.hidden=true;
    pickerToggle.setAttribute('aria-expanded','false');
  }
  function chooseTaskMaterial(choice){
    current=choice;
    pickerToggle.textContent=choice?.material||'选择材质';
  }
  function paintTaskOptions(){
    pickerOptions.replaceChildren();
    for(const [source,label] of [['scene','场景材质'],['library','参考库材质'],['new','新材质']]){
      const choices=materialChoices.filter(choice=>choice.material_source===source);
      if(!choices.length){
        if(source==='scene'){
          const hint=document.createElement('p');
          hint.className='hint';
          hint.textContent='出一轮小样后可选场景材质';
          pickerOptions.append(hint);
        }
        continue;
      }
      const group=document.createElement('div');
      group.setAttribute('role','group');
      group.setAttribute('aria-label',label);
      const heading=document.createElement('strong');
      heading.textContent=label;
      group.append(heading);
      for(const choice of choices){
        const button=document.createElement('button');
        button.type='button';
        button.textContent=choice.material;
        button.onclick=()=>{
          chooseTaskMaterial(choice);
          if(selected>=0){Object.assign(regions[selected],choice);paint();}
          closePicker();pickerToggle.focus();
        };
        group.append(button);
      }
      pickerOptions.append(group);
    }
    const add=document.createElement('button');
    add.type='button';
    add.textContent='新材质…';
    add.onclick=()=>{
      closePicker();
      form.hidden=false;
      $('annotate-material-name').focus();
    };
    pickerOptions.append(add);
  }
  pickerToggle.onclick=()=>{
    if(!pickerOptions.hidden){closePicker();return;}
    paintTaskOptions();
    pickerOptions.hidden=false;
    pickerToggle.setAttribute('aria-expanded','true');
  };
  dialog.addEventListener('pointerdown',event=>{if(!picker.contains(event.target))closePicker();});
  function tool(next){
    mode=next;
    stage.classList.toggle('pan',mode==='pan'||space);
    $('annotate-draw').setAttribute('aria-pressed',String(mode==='draw'));
    $('annotate-pan').setAttribute('aria-pressed',String(mode==='pan'));
  }
  function point(event){
    const rect=frame.getBoundingClientRect();
    return {x:clamp((event.clientX-rect.left)/rect.width,0,1),y:clamp((event.clientY-rect.top)/rect.height,0,1)};
  }
  function insideImage(event){
    const rect=frame.getBoundingClientRect();
    return event.clientX>=rect.left&&event.clientX<=rect.right&&event.clientY>=rect.top&&event.clientY<=rect.bottom;
  }
  stage.onpointerdown=event=>{
    if(busy||gesture||!image.naturalWidth||event.target.closest('[data-remove]')||event.button>2)return;
    if(mode==='pan'||space||event.button!==0){
      gesture={kind:'pan',startX:event.clientX,startY:event.clientY,panX,panY};
    }else{
      if(!insideImage(event)){selectRegion(-1);return;}
      const start=point(event),hit=event.target.closest('[data-region]');
      if(hit){
        const index=Number(hit.dataset.region),box=[...regions[index].box],corner=event.target.dataset.corner;
        selectRegion(index);
        gesture={kind:corner?'resize':'move',index,start,box,corner};
      }else if(regions.length<100&&(taskOptions?current:select.value)){
        const choice=taskOptions?current:{material:select.value};
        const index=regions.push({...choice,box:[start.x,start.y,0,0]})-1;
        selectRegion(index);
        gesture={kind:'draw',index,start};
      }else return;
    }
    event.preventDefault();
    stage.focus({preventScroll:true});
    gesture.pointer=event.pointerId;
    stage.setPointerCapture(event.pointerId);
  };
  stage.onpointermove=event=>{
    if(gesture?.pointer!==event.pointerId)return;
    const {kind,index,start,box,corner}=gesture;
    if(kind==='pan'){
      panX=gesture.panX+event.clientX-gesture.startX;
      panY=gesture.panY+event.clientY-gesture.startY;
      position();return;
    }
    const end=point(event);
    if(kind==='draw')regions[index].box=boxBetween(start,end);
    else{
      const [x,y,w,h]=box;
      if(kind==='move'){
        regions[index].box=[clamp(x+end.x-start.x,0,1-w),clamp(y+end.y-start.y,0,1-h),w,h];
      }else{
        const [right,bottom]=corners[corner];
        const minW=1/image.naturalWidth,minH=1/image.naturalHeight;
        const newX=clamp(x+right*w+end.x-start.x,right?x+minW:0,right?1:x+w-minW);
        const newY=clamp(y+bottom*h+end.y-start.y,bottom?y+minH:0,bottom?1:y+h-minH);
        regions[index].box=boxBetween({x:x+(1-right)*w,y:y+(1-bottom)*h},{x:newX,y:newY});
      }
    }
    paint();
  };
  function endGesture(event){
    if(gesture?.pointer!==event.pointerId)return;
    const {kind,index,box}=gesture;
    gesture=null;
    if(kind==='draw'){
      const [, ,w,h]=regions[index].box,rect=frame.getBoundingClientRect();
      if(event.type!=='pointerup'||w*rect.width<4||h*rect.height<4)removeRegion(index);
    }else if(box&&event.type==='pointercancel'){
      regions[index].box=box;paint();
    }
  }
  stage.onpointerup=stage.onpointercancel=stage.onlostpointercapture=endGesture;
  stage.oncontextmenu=event=>event.preventDefault();
  stage.addEventListener('wheel',event=>{
    event.preventDefault();
    if(gesture||!image.naturalWidth)return;
    const rect=stage.getBoundingClientRect(),next=clamp(zoom*Math.exp(-event.deltaY*.002),1,8);
    const x=event.clientX-(rect.left+rect.width/2),y=event.clientY-(rect.top+rect.height/2);
    panX=x-(x-panX)*next/zoom;
    panY=y-(y-panY)*next/zoom;
    zoom=next;
    position();
  },{passive:false});
  stage.onclick=event=>{
    const button=event.target.closest('[data-remove]');
    if(button&&!busy)removeRegion(Number(button.dataset.remove));
  };
  dialog.onkeydown=event=>{
    if(event.key==='Escape'&&!pickerOptions.hidden){event.preventDefault();closePicker();pickerToggle.focus();return;}
    if(event.target.closest('input,select,textarea')||busy)return;
    if(event.key==='Delete'&&selected>=0&&!gesture){event.preventDefault();removeRegion(selected);}
    if(event.code==='Space'&&event.target===stage){event.preventDefault();space=true;tool(mode);}
  };
  dialog.onkeyup=event=>{if(event.code==='Space'){space=false;tool(mode);}};
  select.onchange=()=>{if(selected>=0){regions[selected].material=select.value;paint();}};
  $('annotate-draw').onclick=()=>{tool('draw');selectRegion(-1);};
  $('annotate-pan').onclick=()=>tool('pan');
  $('annotate-zoom').onclick=()=>{zoom=1;panX=panY=0;position();};
  $('annotate-new-material').onclick=()=>{
    form.hidden=!form.hidden;
    if(!form.hidden)$('annotate-material-name').focus();
  };
  async function submit(operation){
    if(busy)return;
    error.hidden=true;busy=true;
    dialog.querySelectorAll('[data-annotate-lock]').forEach(el=>el.disabled=true);
    try{await operation();}
    catch(failure){error.textContent=failure.message;error.hidden=false;}
    finally{
      busy=false;
      dialog.querySelectorAll('[data-annotate-lock]').forEach(el=>el.disabled=false);
    }
  }
  form.onsubmit=event=>{
    event.preventDefault();
    const name=$('annotate-material-name').value.trim();
    submit(async()=>{
      if(taskOptions){
        if(!name)throw new Error('请输入材质名称。');
        let choice=materialChoices.find(value=>value.material_source==='new'&&value.material===name);
        if(!choice){
          choice={material:name,material_source:'new'};
          materialChoices.push(choice);keys.push(name);
        }
        chooseTaskMaterial(choice);
        if(selected>=0)Object.assign(regions[selected],choice);
        paint();form.reset();form.hidden=true;
        return;
      }
      const library=await api('/api/refs/materials',{name,create:true});
      keys=Object.keys(library.materials);
      options(name);
      if(selected>=0)regions[selected].material=name;
      paint();form.reset();form.hidden=true;
    });
  };
  $('annotate-save').onclick=()=>submit(async()=>{
    if(taskOptions)await taskOptions.save(regions);
    else if(entry.file)await api('/api/refs/annotated',{action:'update',file:entry.file,regions});
    else{
      const metadata=JSON.stringify({source:entry.source,regions})+'\n';
      await uploadBlob('/api/refs/annotated/upload',new Blob([metadata,blob]),{fallback:'保存标注失败'});
    }
    dialog.close('changed');
  });
  $('annotate-delete').onclick=()=>confirm.showModal();
  $('annotate-delete-cancel').onclick=()=>confirm.close();
  $('annotate-delete-confirm').onclick=()=>{
    confirm.close();
    submit(async()=>{
      await api('/api/refs/annotated',{action:'delete',file:entry.file});
      dialog.close('changed');
    });
  };
  $('annotate-cancel').onclick=()=>dialog.close();
  dialog.oncancel=event=>{if(busy)event.preventDefault();};

  async function open(next,materials,initial=-1,sourceBlob=null,task=null){
    entry=next;blob=sourceBlob;keys=Object.keys(materials);
    taskOptions=task;
    regions=structuredClone(entry.regions||[]);
    picker.hidden=!task;
    select.hidden=Boolean(task);
    $('annotate-library-label').hidden=Boolean(task);
    $('annotate-new-material').hidden=Boolean(task);
    $('annotate-library-choice').hidden=!task;
    closePicker();
    if(task){
      const choices=(source,names)=>names.map(material=>({material,material_source:source}));
      const existing=(source)=>regions.filter(r=>r.material_source===source).map(r=>r.material);
      materialChoices=[
        ...choices('scene',[...new Set([...task.sceneMaterials,...existing('scene')])]),
        ...choices('library',keys),
        ...choices('new',[...new Set([...task.newMaterials,...existing('new')])])
      ];
      keys=[...new Set(materialChoices.map(choice=>choice.material))];
      // 从参考库点选进来时预选这张图所属的材质（场景里有同名材质时优先场景材质，排在前面）
      chooseTaskMaterial(materialChoices.find(choice=>task.defaultMaterial&&choice.material===task.defaultMaterial)||materialChoices[0]);
    }else options(keys[0]);
    zoom=1;panX=panY=0;space=false;gesture=null;
    tool('draw');form.reset();form.hidden=true;error.hidden=true;
    selectRegion(initial);
    $('annotate-title').textContent=task?entry.name:entry.source.replaceAll('\\','/').split('/').pop();
    $('annotate-delete').hidden=Boolean(task)||!entry.file;
    const objectUrl=blob?URL.createObjectURL(blob):null;
    try{
      image.src=objectUrl||fileUrl(task?task.path:entry.file,'');
      await image.decode().catch(()=>{throw new Error('不能读取这张图片。');});
      dialog.returnValue='';
      const closed=new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue),{once:true}));
      dialog.showModal();position();stage.focus();
      return await closed;
    }finally{
      image.removeAttribute('src');
      if(objectUrl)URL.revokeObjectURL(objectUrl);
      blob=null;
    }
  }
  return {open};
}
