// fitPose 与 describePose 从柜体预演台复制并适配为 PF1 毫米；不依赖原项目。
export const RAD = Math.PI / 180;
export const VERTICAL_CORRECTION_MAX_DEG = 45;
export const canCorrectVertically = elevation => Math.abs(elevation) <= VERTICAL_CORRECTION_MAX_DEG + 1e-10;
export const clamp = (v, min, max) => Math.max(min, Math.min(max, v));
const dot = (a,b) => a.reduce((s,n,i)=>s+n*b[i],0);
export const pf1ToThree = p => [...p];
export const threeToPf1 = p => [...p];
export const pf1ToMax = ([x,y,z]) => [x,-z,y];
export const maxToPf1 = ([x,y,z]) => [x,z,-y];
export const horizontalFov = (vertical,aspect) => 2*Math.atan(Math.tan(vertical*RAD/2)*aspect)/RAD;
export const focalLengthMm = fov => 50*Math.tan(27*RAD/2)/Math.tan(fov*RAD/2);
export const fovFromFocal = mm => 2*Math.atan(50*Math.tan(27*RAD/2)/mm)/RAD;
export const orientationForRatio = ratio => ratio>1.3?'landscape':'portrait';
export function corners(box) {
  return Array.from({length:8},(_,mask)=>box.min.map((v,i)=>mask>>i&1?box.max[i]:v));
}
export function toSpherical(pose) {
  const [x,y,z]=pose.position_mm.map((n,i)=>n-pose.target_mm[i]);
  return {radius:Math.hypot(x,y,z),azimuth:Math.atan2(x,z)/RAD,elevation:Math.atan2(y,Math.hypot(x,z))/RAD};
}
export function fromSpherical(target,radius,azimuth,elevation,fovY=27,verticalCorrection=false) {
  const a=azimuth*RAD,e=elevation*RAD;
  const back=[Math.sin(a)*Math.cos(e),Math.sin(e),Math.cos(a)*Math.cos(e)];
  return {frame:'PF1',position_mm:target.map((n,i)=>n+radius*back[i]),target_mm:[...target],fov_y_deg:fovY,
    azimuth_deg:azimuth,elevation_deg:elevation,distance_mm:radius,vertical_correction:verticalCorrection};
}
export function basis(azimuth,elevation) {
  const a=azimuth*RAD,e=elevation*RAD;
  return {back:[Math.sin(a)*Math.cos(e),Math.sin(e),Math.cos(a)*Math.cos(e)],
    right:[Math.cos(a),0,-Math.sin(a)],up:[-Math.sin(a)*Math.sin(e),Math.cos(e),-Math.cos(a)*Math.sin(e)]};
}
export function verticalProjection(pose,s=toSpherical(pose)) {
  if(pose.vertical_correction!==true||!canCorrectVertically(s.elevation))return null;
  const [x,y,z]=pose.target_mm.map((n,i)=>n-pose.position_mm[i]);
  const depth=Math.hypot(x,z);
  const centerSlope=y/depth;
  return {target:[pose.target_mm[0],pose.position_mm[1],pose.target_mm[2]],depth,centerSlope,
    offsetY:centerSlope/Math.tan(pose.fov_y_deg*RAD/2)};
}
export function fitPose({box,azimuth=35,elevation=12,fovY=27,aspect=3/4,margin=.1,verticalCorrection=false}) {
  const target=box.min.map((n,i)=>(n+box.max[i])/2);
  const corrected=verticalCorrection&&canCorrectVertically(elevation);
  const {back,right,up}=basis(azimuth,corrected?0:elevation);
  const ty=Math.tan(fovY*RAD/2)*(1-2*margin),tx=ty*aspect;
  const slope=corrected?Math.tan(elevation*RAD):0;
  let radius=.001;
  for(const point of corners(box)) {
    const offset=point.map((n,i)=>n-target[i]),z=dot(offset,back);
    // 离轴投影以原目标为中心时，竖向分子还要扣掉深度差造成的偏移。
    const y=corrected?offset[1]-z*slope:dot(offset,up);
    radius=Math.max(radius,z+Math.abs(dot(offset,right))/tx,z+Math.abs(y)/ty);
    if(corrected)radius=Math.max(radius,z+.001);
  }
  if(corrected)radius/=Math.cos(elevation*RAD);
  return fromSpherical(target,radius,azimuth,elevation,fovY,verticalCorrection);
}
export function projectPoint(point,pose,aspect) {
  const s=toSpherical(pose),correction=verticalProjection(pose,s);
  const {back,right,up}=basis(s.azimuth,correction?0:s.elevation);
  const offset=point.map((n,i)=>n-pose.target_mm[i]),z=dot(offset,back);
  const depth=(correction?.depth??s.radius)-z,ty=Math.tan(pose.fov_y_deg*RAD/2);
  const y=correction?offset[1]+z*correction.centerSlope:dot(offset,up);
  return depth>0?[dot(offset,right)/(depth*ty*aspect),y/(depth*ty),depth]:[Infinity,Infinity,depth];
}
export function projectedOrientation(box,azimuth,elevation) {
  const {right,up}=basis(azimuth,elevation),points=corners(box);
  const xs=points.map(p=>dot(p,right)),ys=points.map(p=>dot(p,up));
  return orientationForRatio((Math.max(...xs)-Math.min(...xs))/Math.max(1e-9,Math.max(...ys)-Math.min(...ys)));
}
export function describePose(pose) {
  const s=toSpherical(pose),a=Math.round(s.azimuth*1e10)/1e10,e=Math.round(s.elevation*1e10)/1e10;
  const abs=Math.abs(a),side=a>=0?'右':'左';
  const horizontal=abs<8?'正前方':abs<65?`${side}前方约 ${Math.round(abs)}°`:abs<=115?`${side}侧`:`${side}后方`;
  const n=Math.round(Math.abs(e));
  const vertical=Math.abs(e)<4?'平视':e<0?`仰视 ${n}°`:e<20?`略俯视 ${n}°`:e<=55?`俯视 ${n}°`:`顶视 ${n}°`;
  return `${horizontal}、${vertical}${verticalProjection(pose,s)?' · 竖线校正':''}`;
}
