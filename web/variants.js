// 识别一个 Max 场景里有几个柜子（款式）：按空间分堆。
// 用户的模型里图层按部件类型分（板件/玻璃/五金…）、组按子柜分，都不能代表「一个柜子」；
// 并排放的几个柜子之间有空隙，同一个柜子的板件互相贴着——所以按包围盒是否相接来并堆。
import {LoadingManager,Texture,Box3,Vector3} from 'three';
import {FBXLoader} from 'three/addons/loaders/FBXLoader.js';
import {materialKind,finishColorOf} from './finishes.js';

const TOLERANCE=.006;      // 相接容差：场景对角线的 0.6%（约 1–2 厘米）
const CRUMB=.02;           // 体积不到最大堆 2% 的碎块并到最近的柜子
const NAME_LIMIT=40;

/** 解析 FBX（不加载贴图），返回 [{id,name,nodes,bbox_mm,colors}]，按从左到右排序。 */
export function detectVariants(buffer){
  const manager=new LoadingManager();
  manager.addHandler(/.*/,{path:'',setPath(){return this;},load(url){if(String(url).startsWith('blob:'))URL.revokeObjectURL(url);return new Texture();}});
  const warn=console.warn;console.warn=()=>{};
  let asset;try{asset=new FBXLoader(manager).parse(buffer,'');}finally{console.warn=warn;}
  asset.updateMatrixWorld(true);
  const unit=(asset.userData.unitScaleFactor>0?asset.userData.unitScaleFactor:1)*10;
  const meshes=[];
  asset.traverse(o=>{if(o.isMesh&&o.name)meshes.push({name:o.name,box:new Box3().setFromObject(o),mats:[].concat(o.material).filter(Boolean)});});
  if(!meshes.length)return [];
  const all=new Box3();for(const m of meshes)all.union(m.box);
  const tol=all.getSize(new Vector3()).length()*TOLERANCE;

  // 并查集：包围盒外扩容差后相交即同一个柜子
  const parent=meshes.map((_,i)=>i);
  const find=i=>{while(parent[i]!==i)i=parent[i]=parent[parent[i]];return i;};
  const grown=meshes.map(m=>m.box.clone().expandByScalar(tol));
  const order=meshes.map((_,i)=>i).sort((a,b)=>grown[a].min.x-grown[b].min.x);
  for(let p=0;p<order.length;p++){
    const i=order[p];
    for(let q=p+1;q<order.length;q++){
      const j=order[q];
      if(grown[j].min.x>grown[i].max.x)break;        // 按 x 排序后提前结束
      if(grown[i].intersectsBox(grown[j]))parent[find(i)]=find(j);
    }
  }
  let groups=new Map();
  meshes.forEach((m,i)=>{const r=find(i);if(!groups.has(r))groups.set(r,[]);groups.get(r).push(m);});
  groups=[...groups.values()].map(list=>{const box=new Box3();list.forEach(m=>box.union(m.box));return {list,box};});

  // 碎块（散落的小五金等）并到中心最近的大堆
  const volume=b=>{const s=b.getSize(new Vector3());return Math.max(s.x*s.y*s.z,1e-9);};
  const biggest=Math.max(...groups.map(g=>volume(g.box)));
  const major=groups.filter(g=>volume(g.box)>=biggest*CRUMB),minor=groups.filter(g=>volume(g.box)<biggest*CRUMB);
  for(const g of minor){
    const c=g.box.getCenter(new Vector3());
    const near=major.reduce((a,b)=>a.box.distanceToPoint(c)<=b.box.distanceToPoint(c)?a:b);
    near.list.push(...g.list);near.box.union(g.box);
  }
  // 同一产品的吊柜 + 地柜（上下分体，中间有空隙）不能拆成两个柜子：
  // ① 水平投影（x、z）有重叠的堆合并（吊柜在地柜正上方）；② 离地悬空的堆并到水平距离最近的落地堆。
  // 并排摆放的几个款式水平投影互不重叠、都落地，仍然分开。
  const floor=all.min.y,height=Math.max(all.max.y-all.min.y,1e-9);
  const footprint=(a,b)=>a.min.x<=b.max.x+tol&&b.min.x<=a.max.x+tol&&a.min.z<=b.max.z+tol&&b.min.z<=a.max.z+tol;
  const absorb=(into,from)=>{into.list.push(...from.list);into.box.union(from.box);major.splice(major.indexOf(from),1);};
  for(let merged=true;merged;){
    merged=false;
    outer:for(const a of major)for(const b of major){
      if(a!==b&&footprint(a.box,b.box)){absorb(a,b);merged=true;break outer;}
    }
  }
  const grounded=g=>g.box.min.y-floor<=height*.1;
  if(major.some(grounded))for(const g of major.filter(g=>!grounded(g))){
    const c=g.box.getCenter(new Vector3()),flat=b=>{const d=b.box.getCenter(new Vector3());return Math.hypot(d.x-c.x,d.z-c.z);};
    absorb(major.filter(grounded).reduce((a,b)=>flat(a)<=flat(b)?a:b),g);
  }
  major.sort((a,b)=>a.box.getCenter(new Vector3()).x-b.box.getCenter(new Vector3()).x);

  const names=new Set();
  return major.map((g,k)=>{
    const {name,colors}=describe(g.list);
    let label=name||`柜子 ${k+1}`;
    if(names.has(label))label=`${label} ${k+1}`;
    names.add(label);
    const mm=v=>v.toArray().map(n=>Math.round(n*unit*10)/10);
    return {id:`v${k+1}`,name:label.slice(0,NAME_LIMIT),nodes:[...new Set(g.list.map(m=>m.name))],
      bbox_mm:{min:mm(g.box.min),max:mm(g.box.max)},colors};
  });
}

// 名字 = 体积占比最大的两种饰面材质；色块 = 这两种饰面的近似色
function describe(list){
  const weight=new Map();
  for(const m of list){
    const s=m.box.getSize(new Vector3()),area=s.x*s.y+s.y*s.z+s.x*s.z;
    for(const mat of m.mats){
      const raw=String(mat.name||'').trim();
      if(!raw||materialKind(raw)!=='finish')continue;
      const clean=raw.replace(/[（(].*?[)）]/g,'').replace(/[-—_#].*$/,'').replace(/[<>:"/\\|?*]/g,'').trim();
      if(!clean||/^(材质|material|默认|default|PF_)/i.test(clean))continue;
      weight.set(clean,(weight.get(clean)||0)+area/m.mats.length);
    }
  }
  const top=[...weight.entries()].sort((a,b)=>b[1]-a[1]).slice(0,2).map(([n])=>n);
  const colors=top.map(n=>finishColorOf(n)).filter(c=>c!==null).map(c=>'#'+c.toString(16).padStart(6,'0'));
  return {name:top.join('+'),colors};
}

/** 重新识别时尽量沿用旧的 id 和名字（按节点名重合度配对），让已建的兄弟任务继续对得上。 */
export function matchVariants(found,previous=[]){
  const used=new Set();
  const overlap=(a,b)=>{const s=new Set(a);let n=0;for(const x of b)if(s.has(x))n++;return n/Math.max(1,Math.min(a.length,b.length));};
  let next=Math.max(0,...previous.map(v=>Number(String(v.id).replace(/\D/g,''))||0))+1;
  return found.map(v=>{
    let best=null,score=.5;
    for(const old of previous){if(used.has(old.id))continue;const s=overlap(v.nodes,old.nodes);if(s>score){best=old;score=s;}}
    if(best){used.add(best.id);return {...v,id:best.id,name:best.name};}
    return {...v,id:`v${next++}`};
  });
}

/** 发给服务端的字段（颜色只在网页里用）。 */
export const variantPayload=v=>({id:v.id,name:v.name,nodes:v.nodes,bbox_mm:v.bbox_mm});
