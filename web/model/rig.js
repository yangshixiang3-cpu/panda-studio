import {AnimationMixer, LoopOnce, Matrix4, Quaternion, Vector3} from 'three';

const TYPES=new Set(['door','flap','drawer','slide','rotate','telescope','fold','custom']);
const isRig=o=>!o.isMesh&&o.name.startsWith('PZ__');
const vec=a=>new Vector3().fromArray(a);
const degrees=180/Math.PI;

/** 在网格摘离原树前，保存成员、父链及原始世界空间累计变换。 */
export function captureRig(root) {
  const nodes=[];
  root.traverse(o=>{if(isRig(o))nodes.push(o);});
  if(!nodes.length)return null;
  const warnings=[],clip=root.animations?.[0];
  if((root.animations?.length??0)>1)warnings.push('模型含多个动画段，绑定动作只读取第一个动画段。');
  const records=nodes.map(node=>{
    const [,type,...name]=node.name.split('__');
    let parent=node.parent;
    while(parent&&!isRig(parent))parent=parent.parent;
    const members=[];
    const visit=o=>{for(const c of o.children){if(isRig(c))continue;if(c.isMesh&&c.userData.previzKey)members.push(c.userData.previzKey);visit(c);}};
    visit(node);
    return {node,parent,name:name.join('__')||node.name,type:TYPES.has(type)?type:'custom',members,samples:[]};
  });
  const index=new Map(nodes.map((n,i)=>[n,i]));
  const finish=timing=>({warnings,...(timing?{timing}:{}),records:records.map(r=>({name:r.name,type:r.type,parent:index.get(r.parent)??null,members:r.members,samples:r.samples}))});
  if(!clip||!clip.tracks.length){warnings.push('模型的 PZ 节点没有可读取的动画段。');return finish();}
  // AnimationClip 的时间单位就是秒；从实际首尾关键帧读取，不能按帧号加减 1。
  const start=Math.min(...clip.tracks.map(t=>t.times[0])),end=Math.max(...clip.tracks.map(t=>t.times.at(-1)));
  if(!Number.isFinite(start)||!Number.isFinite(end)){warnings.push('绑定动画的关键帧时间无效。');return finish();}
  if(end-start>4.001)warnings.push('绑定动画超过 4 秒，只读取约定的前四步。');
  const mixer=new AnimationMixer(root),action=mixer.clipAction(clip);
  action.setLoop(LoopOnce,1);action.clampWhenFinished=true;action.play();
  const sample=time=>{mixer.setTime(time);root.updateMatrixWorld(true);};
  sample(start);
  const rest=[];
  root.traverse(node=>rest.push({node,position:node.position.clone(),quaternion:node.quaternion.clone(),scale:node.scale.clone()}));
  for(const r of records){r.p0=r.node.parent?.matrixWorld.clone()??new Matrix4();r.p0Inv=r.p0.clone().invert();r.l0Inv=r.node.matrix.clone().invert();}
  try {
    for(let step=0;step<Math.min(4,Math.ceil(end-start-1e-6));step++){
      for(let j=0;j<=8;j++){
        sample(Math.min(end,start+step+j/8));
        for(const r of records){
          if(!r.samples[step])r.samples[step]=[];
          r.samples[step].push(r.p0.clone().multiply(r.node.matrix).multiply(r.l0Inv).multiply(r.p0Inv));
        }
      }
    }
  } finally {
    action.stop();mixer.uncacheRoot(root);
    for(const r of rest){r.node.position.copy(r.position);r.node.quaternion.copy(r.quaternion);r.node.scale.copy(r.scale);r.node.updateMatrix();}
    root.updateMatrixWorld(true);
  }
  return finish({start,end});
}

function rotationQuaternion(matrix) {
  const q=new Quaternion();matrix.decompose(new Vector3(),q,new Vector3());q.normalize();
  if(q.w<0)q.set(-q.x,-q.y,-q.z,-q.w);
  return q;
}

function readStep(samples,center,minDistance) {
  let previous=new Matrix4(),axis=null,angle=0;
  for(let i=1;i<samples.length;i++){
    const q=rotationQuaternion(previous.clone().invert().multiply(samples[i]));
    const v=new Vector3(q.x,q.y,q.z);
    if(v.length()>1e-7){
      // 轴的正负等价，首个增量选稳定的正向，随后沿它累加带符号转角。
      if(!axis){axis=v.clone().normalize();const components=axis.toArray(),largest=components.reduce((a,b)=>Math.abs(a)>Math.abs(b)?a:b);if(largest<0)axis.negate();}
      angle+=2*Math.atan2(v.dot(axis),q.w);
    }
    previous=samples[i];
  }
  if(Math.abs(angle)*degrees<0.05){
    const delta=new Vector3().setFromMatrixPosition(samples.at(-1)),distance=delta.length();
    return distance<minDistance?null:{kind:'translate',axis:delta.divideScalar(distance).toArray(),distance,weight:1};
  }
  // 用首个非零旋转样本解 (I-R)p=t，再沿转轴投影到距成员中心最近的位置。
  const first=samples.slice(1).find(m=>{const q=rotationQuaternion(m);return Math.hypot(q.x,q.y,q.z)>1e-7;});
  const q=rotationQuaternion(first),theta=2*Math.atan2(new Vector3(q.x,q.y,q.z).dot(axis),q.w);
  const t=new Vector3().setFromMatrixPosition(first);
  const pivot=t.clone().add(axis.clone().cross(t).multiplyScalar(1/Math.tan(theta/2))).multiplyScalar(0.5);
  pivot.addScaledVector(axis,center.clone().sub(pivot).dot(axis));
  return {kind:'rotate',axis:axis.toArray(),pivot:pivot.toArray(),angle:angle*degrees,weight:1};
}

function outward(part,infos,productCenter) {
  if(['drawer','telescope'].includes(part.type)){
    const step=part.motion.steps[0];
    if(step.kind!=='translate')return null;
    const v=vec(step.axis).setY(0);return v.length()>1e-6?v.normalize().toArray():null;
  }
  if(!['door','flap'].includes(part.type))return null;
  const main=infos.get(part.meshKeys[0]);
  if(!main||main.kind!=='panel')return null;
  const normal=vec(main.thinAxis).setY(0);
  if(normal.length()<1e-6)return null;
  normal.normalize();
  const offset=vec(main.obb.center).sub(productCenter).dot(normal);
  if(Math.abs(offset)<0.001)return null;
  return normal.multiplyScalar(Math.sign(offset)).toArray();
}

/** 原始取样不变；每次按当前归一化空间和主体成员重新生成部件。 */
export function normalizeRig(raw,matrix,infos) {
  if(!raw)return null;
  const warnings=[...raw.warnings],byKey=new Map(infos.map(i=>[i.key,i]));
  const n=new Matrix4().fromArray(matrix),inv=n.clone().invert(),parts=[],kept=new Map();
  const unitScale=new Vector3().setFromMatrixColumn(n,0).length();
  for(const [index,r] of raw.records.entries()){
    const members=r.members.filter(k=>byKey.has(k)).sort((a,b)=>(byKey.get(b).volume??0)-(byKey.get(a).volume??0));
    if(!members.length){warnings.push(`「${r.name}」的成员网格全不在主体分组，已略过该绑定部件。`);continue;}
    const center=members.reduce((v,k)=>v.add(vec(byKey.get(k).obb.center)),new Vector3()).divideScalar(members.length);
    const steps=r.samples.map(samples=>{
      const base=samples[0].clone().invert();
      const step=readStep(samples.map(c=>base.clone().multiply(c)),center.clone().applyMatrix4(inv),0.0005/unitScale);
      if(!step)return null;
      step.axis=vec(step.axis).transformDirection(n).toArray();
      if(step.kind==='translate')step.distance*=unitScale;
      else step.pivot=vec(step.pivot).applyMatrix4(n).toArray();
      return step;
    }).filter(Boolean);
    if(!steps.length){warnings.push(`「${r.name}」没有有效绑定动作步骤，已略过。`);continue;}
    const part={id:`p${parts.length+1}`,name:r.name,type:r.type,meshKeys:members,parentId:null,motion:{steps},outward:null,source:'rig',confidence:1,reason:'模型自带绑定动作',observed:null};
    parts.push(part);kept.set(index,part);
  }
  const productBox=infos.reduce((b,i)=>({min:b.min.map((v,k)=>Math.min(v,i.aabb.min[k])),max:b.max.map((v,k)=>Math.max(v,i.aabb.max[k]))}),{min:[Infinity,Infinity,Infinity],max:[-Infinity,-Infinity,-Infinity]});
  const productCenter=vec(productBox.min.map((v,k)=>(v+productBox.max[k])/2));
  for(const [index,part] of kept){
    let parent=raw.records[index].parent;
    while(parent!==null&&!kept.has(parent))parent=raw.records[parent].parent;
    part.parentId=kept.get(parent)?.id??null;
    part.outward=outward(part,byKey,productCenter);
  }
  return {parts,warnings};
}
