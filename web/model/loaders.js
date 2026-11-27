import {LoadingManager,Texture,MeshStandardMaterial,Box3,Vector3,Matrix4,Group,SRGBColorSpace} from 'three';
import {FBXLoader} from 'three/addons/loaders/FBXLoader.js';
import {captureRig,normalizeRig} from './rig.js';
// 饰面色表放在不依赖 three 的 finishes.js，单测和其他模块也能用
import {finishes} from '../finishes.js';
import {withFilteredFbxWarnings} from '../variants.js';
export {materialKind,finishColorOf} from '../finishes.js';

function previewMaterial(source,envMap) {
  const name=source?.name||'';
  const opacity=Number.isFinite(source?.opacity)?source.opacity:1;
  const options={name,opacity,transparent:!!source?.transparent||opacity<1};
  if(/镜|mirror/i.test(name)){
    Object.assign(options,{color:0xe8e8e8,roughness:.04,metalness:1,envMap});
  }else if(/玻|glass/i.test(name)){
    const color=/茶色|茶玻/.test(name)?0x5b4434:/灰玻|灰色玻璃/.test(name)?0x5f6466:0xdfe7e6;
    Object.assign(options,{color,roughness:.05,envMap,transparent:true,depthWrite:false,
      opacity:opacity<1?Math.max(.25,Math.min(.6,opacity)):.35});
  }else if(/五金|金属|不锈钢|银|铝|拉手|金|铜|香槟|steel|metal/i.test(name)){
    const color=/铜|香槟|(?<!五)金(?!属)/.test(name)?0xb8955a:0xc8c9cb;
    Object.assign(options,{color,roughness:.3,metalness:.9,envMap});
  }else{
    const finish=finishes.find(([word])=>name.includes(word));
    Object.assign(options,{color:finish?.[1]??0xb3aea6,roughness:finish?.[2]??.7});
  }
  return new MeshStandardMaterial(options);
}

function textureSource(url,taskId) {
  if(/^(blob|data):/.test(url))return {url};
  const name=url.split(/[/\\]/).pop();
  if(!taskId||!/\.(jpe?g|png|webp|bmp)$/i.test(name)||name.includes('..')||/[:<>|"?*\x00-\x1f]/.test(name))return null;
  return {name,url:'/api/tasks/'+encodeURIComponent(taskId)+'/texture?name='+encodeURIComponent(name)};
}

function fbxTextureConnections(data) {
  const bytes=new Uint8Array(data),decoder=new TextDecoder();
  const materialIds=new Set(),children=new Map();
  const add=(id,parent,slot)=>{
    if(!children.has(parent))children.set(parent,[]);
    children.get(parent).push({id,slot});
  };
  if(decoder.decode(bytes.subarray(0,20))!=='Kaydara FBX Binary  '){
    const ascii=decoder.decode(bytes);
    if(ascii.includes('|baseTexmap')){
      for(const [,id] of ascii.matchAll(/^\s*Material:\s*(-?\d+)\s*,/gm))materialIds.add(Number(id));
      for(const [,id,parent,slot] of ascii.matchAll(/^\s*C:\s*"(?:OO|OP)"\s*,\s*(-?\d+)\s*,\s*(-?\d+)(?:\s*,\s*"([^"]*)")?/gm))
        add(Number(id),Number(parent),slot);
    }
    return {materialIds,children};
  }
  const view=new DataView(data),wide=view.getUint32(23,true)>=7500,width=wide?8:4;
  const uint=offset=>wide?Number(view.getBigUint64(offset,true)):view.getUint32(offset,true);
  const string=(offset,length)=>decoder.decode(bytes.subarray(offset,offset+length));
  function* nodes(start,limit){
    for(let offset=start;offset+3*width<limit;){
      const end=uint(offset);
      if(end<=offset||end>limit)break;
      const nameLength=bytes[offset+3*width],nameStart=offset+3*width+1,props=nameStart+nameLength;
      const content=props+uint(offset+2*width);
      if(content>end)break;
      yield {name:string(nameStart,nameLength),props,content,end};
      offset=end;
    }
  }
  function properties({props,content},count){
    const values=[];
    while(props<content&&values.length<count){
      const type=bytes[props++];
      if(type===76&&props+8<=content){ // L
        values.push(Number(view.getBigInt64(props,true)));props+=8;
      }else if(type===73&&props+4<=content){ // I
        values.push(view.getInt32(props,true));props+=4;
      }else if(type===83&&props+4<=content){ // S
        const length=view.getUint32(props,true);props+=4;
        if(props+length>content)break;
        values.push(string(props,length));props+=length;
      }else break;
    }
    return values;
  }
  // 只读对象标识与连接，按节点边界跳过几何、图像和动画数据。
  for(const section of nodes(27,bytes.length)){
    if(section.name==='Objects'){
      for(const node of nodes(section.content,section.end)){
        if(node.name==='Material')materialIds.add(properties(node,1)[0]);
      }
    }else if(section.name==='Connections'){
      for(const node of nodes(section.content,section.end)){
        if(node.name!=='C')continue;
        const [,id,parent,slot]=properties(node,4);
        if(typeof parent==='number')add(id,parent,slot);
      }
    }
  }
  return {materialIds,children};
}

function coronaTextureBindings(data,sources) {
  const bindings=new Map();
  if(!sources.size)return bindings;
  const {materialIds,children}=fbxTextureConnections(data);
  const textures=new Map(Array.from(sources.keys(),texture=>[texture.ID,texture]));
  function bitmap(id,visited){
    if(textures.has(id))return textures.get(id);
    if(visited.has(id))return;
    visited.add(id);
    const inputs=children.get(id)||[];
    const direct=inputs.filter(input=>input.slot?.endsWith('|directTexmap'));
    for(const input of direct.length?direct:inputs){
      const texture=bitmap(input.id,visited);
      if(texture)return texture;
    }
  }
  const materialMaps=new Map();
  for(const id of materialIds){
    const base=children.get(id)?.find(input=>input.slot==='3dsMax|CoronaPhysicalMtlPb|baseTexmap');
    if(base)materialMaps.set(id,bitmap(base.id,new Set()));
  }
  if(materialMaps.size){
    for(const [id,inputs] of children){
      const slots=inputs.filter(input=>materialIds.has(input.id)).map(input=>materialMaps.get(input.id));
      if(slots.some(Boolean))bindings.set(id,slots);
    }
  }
  return bindings;
}

async function loadTextureImage({url,name},signal) {
  let objectUrl;
  try{
    if(name){
      const response=await fetch(url,{signal});
      if(response.status===404)return null;
      if(!response.ok)throw new Error('贴图读取失败（HTTP '+response.status+'）');
      const blob=await response.blob();
      signal.throwIfAborted();
      objectUrl=URL.createObjectURL(blob);
    }
    const image=new Image();
    image.src=objectUrl||url;
    await image.decode();
    signal.throwIfAborted();
    const scale=2048/Math.max(image.naturalWidth,image.naturalHeight);
    if(scale>=1)return image;
    const resized=document.createElement('canvas');
    resized.width=Math.max(1,Math.round(image.naturalWidth*scale));
    resized.height=Math.max(1,Math.round(image.naturalHeight*scale));
    const context=resized.getContext('2d');
    context.imageSmoothingQuality='high';
    context.drawImage(image,0,0,resized.width,resized.height);
    return resized;
  }finally{
    if(objectUrl)URL.revokeObjectURL(objectUrl);
  }
}

/** 单次解析 FBX 并提取 Corona 贴图绑定，不持有原始 ArrayBuffer 引用，供款式检测与预览建模复用。 */
export function parseFbxAsset(data,{taskId}={}) {
  const manager=new LoadingManager(),sources=new Map(),embeddedUrls=new Set();
  manager.addHandler(/.*/,{
    path:'',setPath(path){this.path=path;return this;},
    load(url){
      const texture=new Texture();
      const source=textureSource(url,taskId);
      if(source)sources.set(texture,source);
      if(String(url).startsWith('blob:'))embeddedUrls.add(url);
      return texture;
    }
  });
  function releaseEmbedded(url){
    if(embeddedUrls.delete(url))URL.revokeObjectURL(url);
  }
  let asset;
  try{asset=withFilteredFbxWarnings(()=>new FBXLoader(manager).parse(data,''));}
  catch(error){for(const url of [...embeddedUrls])releaseEmbedded(url);throw error;}
  const coronaBindings=coronaTextureBindings(data,sources);
  let consumed=false;
  return {
    isPreparedFbx:true,asset,sources,embeddedUrls,releaseEmbedded,coronaBindings,
    consume(){consumed=true;},
    abort(){
      if(consumed)return;
      consumed=true;
      for(const url of [...embeddedUrls])releaseEmbedded(url);
      asset.traverse(o=>{
        o.geometry?.dispose?.();
        for(const m of [].concat(o.material||[]))m?.dispose?.();
      });
      for(const texture of sources.keys()){texture.dispose();texture.image=null;}
    },
  };
}

// keepNames：一个场景有多个柜子时，只保留当前柜子的网格（按 Max 节点名），并以它为中心、按它算尺寸。
export function parseFbx(input,{bbox=null,rotation=0,taskId,envMap=null,anisotropy=1,onTexturesChanged,keepNames=null}={}) {
  const prepared=input?.isPreparedFbx?input:parseFbxAsset(input,{taskId});
  prepared.consume();
  const {asset,sources,embeddedUrls,releaseEmbedded,coronaBindings}=prepared;
  const warnings=[],requests=new Map(),materials=new Set(),oldMaterials=new Set(),missingTextures=new Set();
  const controller=new AbortController(),{signal}=controller;
  let index=0; const meshes=[],skipped=[];
  asset.traverse(o=>{
    if(!o.isMesh)return;
    if(!keepNames||keepNames.has(o.name)){o.userData.previzKey=`m${index++}`;meshes.push(o);}
    else skipped.push(o);
  });
  if(!meshes.length){
    for(const url of [...embeddedUrls])releaseEmbedded(url);
    for(const m of skipped){m.geometry?.dispose?.();for(const mat of [].concat(m.material||[]))mat?.dispose?.();}
    for(const texture of sources.keys()){texture.dispose();texture.image=null;}
    throw new Error(keepNames?'预览模型里找不到这个柜子的物体，模型可能改过，请点柜子切换器里的「重新识别柜子」。':'模型里没有可预览的网格。');
  }
  for(const m of skipped){m.geometry?.dispose?.();for(const mat of [].concat(m.material||[]))oldMaterials.add(mat);}
  const raw=captureRig(asset);asset.updateMatrixWorld(true);
  const rawBox=new Box3();for(const m of meshes)rawBox.union(new Box3().setFromObject(m));
  const unit=(asset.userData.unitScaleFactor>0?asset.userData.unitScaleFactor:1)*10;
  const rawSize=rawBox.getSize(new Vector3()),scales=[unit,unit,unit];
  // 执行器记录的是整个场景的尺寸；只显示其中一个柜子时不能拿它校正。
  if(bbox?.size_mm&&!keepNames){
    const expected=[bbox.size_mm.x,bbox.size_mm.y_up,bbox.size_mm.z_depth],actual=rawSize.toArray().map(v=>v*unit);
    if(actual.some((v,i)=>Math.abs(v-expected[i])/expected[i]>.01)){
      expected.forEach((v,i)=>scales[i]=v/rawSize.getComponent(i));warnings.push('模型尺寸与执行器记录相差超过 1%，已按毫米尺寸校正。');
    }
  }
  const center=rawBox.getCenter(new Vector3());
  const n=new Matrix4().makeRotationY(rotation*Math.PI/180)
    .multiply(new Matrix4().makeScale(...scales)).multiply(new Matrix4().makeTranslation(-center.x,-center.y,-center.z));
  const infos=[],base=new Map(),root=new Group();root.name='产品预览';
  for(const m of meshes){
    const matrix=n.clone().multiply(m.matrixWorld);m.geometry.computeBoundingBox();
    const b=m.geometry.boundingBox.clone().applyMatrix4(matrix),size=b.getSize(new Vector3()),key=m.userData.previzKey;
    infos.push({key,aabb:{min:b.min.toArray(),max:b.max.toArray()},obb:{center:b.getCenter(new Vector3()).toArray()},volume:size.x*size.y*size.z});
    base.set(key,matrix.clone());m.matrixAutoUpdate=false;m.matrix.copy(matrix);
    const old=m.material,convert=(mat,slot)=>{
      if(mat)oldMaterials.add(mat);
      const material=previewMaterial(mat,envMap);
      const texture=sources.has(mat?.map)?mat.map:coronaBindings.get(m.ID)?.[slot],source=sources.get(texture);
      materials.add(material);
      if(source){
        if(!requests.has(source.url))requests.set(source.url,{source,targets:new Map()});
        const targets=requests.get(source.url).targets;
        if(!targets.has(texture))targets.set(texture,[]);
        targets.get(texture).push(material);
      }
      return material;
    };
    m.material=Array.isArray(old)?old.map(convert):convert(old,0);root.add(m);
  }
  for(const mat of oldMaterials)mat.dispose();
  const rig=normalizeRig(raw,n.elements,infos),parts=rig?.parts||[];warnings.push(...(rig?.warnings||[]));
  root.updateMatrixWorld(true);const box=new Box3().setFromObject(root);
  const owner=new Map();for(const part of parts)for(const key of part.meshKeys)owner.set(key,part);
  function partMatrix(part,values,cache) {
    if(cache.has(part.id))return cache.get(part.id);
    const parent=parts.find(p=>p.id===part.parentId),matrix=parent?partMatrix(parent,values,cache).clone():new Matrix4();
    const steps=part.motion.steps,total=steps.reduce((s,p)=>s+(p.weight||1),0),u=values[part.name]||0;let elapsed=u*total;
    for(const step of steps){
      const weight=step.weight||1,k=Math.max(0,Math.min(1,elapsed/weight));elapsed-=weight;
      const axis=new Vector3().fromArray(step.axis);
      if(step.kind==='translate')matrix.multiply(new Matrix4().makeTranslation(...axis.multiplyScalar(step.distance*k).toArray()));
      else {const pivot=new Vector3().fromArray(step.pivot);matrix.multiply(new Matrix4().makeTranslation(...pivot.toArray()))
        .multiply(new Matrix4().makeRotationAxis(axis,step.angle*k*Math.PI/180))
        .multiply(new Matrix4().makeTranslation(...pivot.negate().toArray()));}
    }
    cache.set(part.id,matrix);return matrix;
  }
  for(const url of embeddedUrls)if(!requests.has(url))releaseEmbedded(url);
  for(const {source,targets} of requests.values()){
    loadTextureImage(source,signal).then(image=>{
      if(signal.aborted)return;
      if(!image)missingTextures.add(source.name.toLowerCase());
      else for(const [texture,mappedMaterials] of targets){
        // 复用 FBXLoader 已配置的纹理对象，保留 UV 变换和环绕方式。
        texture.image=image;texture.colorSpace=SRGBColorSpace;
        texture.anisotropy=Math.min(8,Math.max(1,anisotropy));texture.needsUpdate=true;
        for(const material of mappedMaterials){
          material.map=texture;material.color.setHex(0xffffff);
          if(material.roughness===.7)material.roughness=.6;
          material.needsUpdate=true;
        }
      }
      onTexturesChanged?.();
    }).catch(error=>{
      if(!signal.aborted)console.warn('预览贴图加载失败。',source.name||'内嵌贴图',error);
    }).finally(()=>releaseEmbedded(source.url));
  }
  return {root,parts,warnings,box:{min:box.min.toArray(),max:box.max.toArray()},size:box.getSize(new Vector3()).toArray(),meshCount:meshes.length,
    get missingTextureCount(){return missingTextures.size;},
    setParts(values){const cache=new Map();for(const mesh of meshes){const key=mesh.userData.previzKey,part=owner.get(key);mesh.matrix.copy(base.get(key));
      if(part)mesh.matrix.premultiply(partMatrix(part,values,cache));}root.updateMatrixWorld(true);},
    currentBox(){const b=new Box3().setFromObject(root);return {min:b.min.toArray(),max:b.max.toArray()};},
    dispose(){
      if(signal.aborted)return;
      controller.abort();
      for(const url of embeddedUrls)releaseEmbedded(url);
      for(const mesh of meshes)mesh.geometry.dispose();
      for(const material of materials)material.dispose();
      for(const texture of sources.keys()){texture.dispose();texture.image=null;}
    }};
}
