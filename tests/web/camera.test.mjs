import test from 'node:test';
import assert from 'node:assert/strict';
import {pf1ToThree,threeToPf1,pf1ToMax,maxToPf1,horizontalFov,fromSpherical,toSpherical,fitPose,corners,projectPoint,orientationForRatio,projectedOrientation,focalLengthMm,fovFromFocal,describePose,verticalProjection,basis} from '../../web/camera.js';

test('PF1 与 three 同轴同毫米，往返不改变输入',()=>{
  const p=[123,456,-789];assert.deepEqual(pf1ToThree(p),p);assert.notEqual(pf1ToThree(p),p);assert.deepEqual(threeToPf1(pf1ToThree(p)),p);
});
test('PF1 到 Max 的竖直与正面方向',()=>{assert.deepEqual(pf1ToMax([1,2,3]),[1,-3,2]);assert.deepEqual(maxToPf1(pf1ToMax([1,2,3])),[1,2,3]);});
test('竖直视场角换算水平视场角，横竖画幅都使用实际比例',()=>{
  assert.ok(Math.abs(horizontalFov(90,4/3)-106.260204708)<1e-8);
  assert.ok(Math.abs(horizontalFov(90,3/4)-73.739795292)<1e-8);assert.equal(horizontalFov(27,1),27);
});
test('方位角正面为零，右转为正，坐标为权威值',()=>{
  const p=fromSpherical([20,30,40],2700,35,12,27);p.azimuth_deg=-999;
  assert.ok(Math.abs(toSpherical(p).azimuth-35)<1e-10);assert.ok(Math.abs(toSpherical(p).elevation-12)<1e-10);
});
for(const aspect of [3/4,4/3])for(const [a,e] of [[0,6],[35,12],[-35,10],[35,35],[180,6],[90,80]]){
  test(`自动取景八角点在 10% 留白线内，且距离最小：${aspect}、${a}°、${e}°`,()=>{
    const box={min:[-1100,-450,-300],max:[1100,450,300]};const p=fitPose({box,azimuth:a,elevation:e,aspect});
    const projected=corners(box).map(c=>projectPoint(c,p,aspect));
    assert.ok(projected.every(q=>q[2]>0&&Math.abs(q[0])<=.80000000001&&Math.abs(q[1])<=.80000000001));
    const nearer=fromSpherical(p.target_mm,toSpherical(p).radius*.999999,a,e,p.fov_y_deg);
    assert.ok(corners(box).some(c=>{const q=projectPoint(c,nearer,aspect);return Math.abs(q[0])>.8||Math.abs(q[1])>.8;}));
  });
}
test('非零包围盒中心保持不偏移',()=>{
  const p=fitPose({box:{min:[100,200,300],max:[700,1000,800]}});assert.deepEqual(p.target_mm,[400,600,550]);
});
test('严格大于 1.3 才使用横版',()=>{assert.equal(orientationForRatio(1.3),'portrait');assert.equal(orientationForRatio(1.3000001),'landscape');assert.equal(orientationForRatio(1.299),'portrait');});
test('按视角投影宽高决定横竖版',()=>{
  const box={min:[-1500,-250,-200],max:[1500,250,200]};assert.equal(projectedOrientation(box,0,0),'landscape');assert.equal(projectedOrientation(box,90,0),'portrait');
});
test('默认透视对应 50 mm，换算可逆',()=>{assert.ok(Math.abs(focalLengthMm(27)-50)<1e-10);for(const mm of [24,50,85,135])assert.ok(Math.abs(focalLengthMm(fovFromFocal(mm))-mm)<1e-10);});
test('角度描述为中文并在边界稳定',()=>{assert.equal(describePose(fromSpherical([0,0,0],3000,35,12)),'右前方约 35°、略俯视 12°');assert.equal(describePose(fromSpherical([0,0,0],3000,0,0)),'正前方、平视');});

test('离轴投影保持水平视轴，目标居中，竖线的横坐标恒定',()=>{
  for(const aspect of [4/3,3/4]){
    const pose=fromSpherical([23,-40,150],3200,35,25,27,true);
    const correction=verticalProjection(pose);
    assert.deepEqual(correction.target,[23,pose.position_mm[1],150]);
    assert.deepEqual(projectPoint(pose.target_mm,pose,aspect),[0,0,correction.depth]);
    const a=projectPoint([430,-540,290],pose,aspect),b=projectPoint([430,640,290],pose,aspect);
    assert.equal(a[0],b[0]);assert.equal(a[2],b[2]);
    assert.ok(Math.abs((b[1]-a[1])*a[2]*Math.tan(27*Math.PI/360)-1180)<1e-10);
    assert.equal(describePose(pose),'右前方约 35°、俯视 25° · 竖线校正');
  }
});

test('从权威坐标求俯仰角，正负 45° 生效，超过则沿用原投影',()=>{
  for(const sign of [-1,1]){
    const boundary=fromSpherical([10,20,30],3600,35,sign*45,27,true);
    boundary.elevation_deg=60;assert.ok(verticalProjection(boundary));
    const over=fromSpherical([10,20,30],3600,35,sign*45.00001,27,true);
    over.elevation_deg=0;assert.equal(verticalProjection(over),null);
    assert.deepEqual(projectPoint([400,500,600],over,4/3),
      projectPoint([400,500,600],{...over,vertical_correction:false},4/3));
    assert.ok(!describePose(over).includes('竖线校正'));
  }
});

test('校正后横竖版均按离轴八角点取景，至少留白 10%',()=>{
  const box={min:[-900,-800,-300],max:[1050,1200,500]};
  const fits=(pose,aspect)=>corners(box).every(point=>{
    const [x,y,depth]=projectPoint(point,pose,aspect);
    return depth>0&&Math.abs(x)<=.80000000001&&Math.abs(y)<=.80000000001;
  });
  for(const aspect of [3/4,4/3])for(const elevation of [-45,0,25,45,70]){
    const options={box,azimuth:35,elevation,fovY:32,aspect};
    const pose=fitPose({...options,verticalCorrection:true});
    assert.ok(fits(pose,aspect));
    const nearer=fromSpherical(pose.target_mm,pose.distance_mm*.999999,35,elevation,32,true);
    assert.ok(!fits(nearer,aspect));
    if(elevation>45)assert.deepEqual(pose,{...fitPose(options),vertical_correction:true});
  }
});

test('缺省及关闭开关时复现原始投影的三个分量',()=>{
  const dot=(a,b)=>a.reduce((total,n,i)=>total+n*b[i],0);
  for(const [azimuth,elevation,aspect,fov] of [[35,25,4/3,27],[-130,-13,3/4,53],[95,80,3/4,18]]){
    const pose=fromSpherical([23,170,-7],2700,azimuth,elevation,fov);
    const s=toSpherical(pose),{back,right,up}=basis(s.azimuth,s.elevation);
    const ty=Math.tan(pose.fov_y_deg*(Math.PI/180)/2);
    for(const point of [[343,456,-100],[720,-430,1700],pose.target_mm,pose.position_mm]){
      const offset=point.map((n,i)=>n-pose.target_mm[i]),depth=s.radius-dot(offset,back);
      const expected=depth>0?[dot(offset,right)/(depth*ty*aspect),dot(offset,up)/(depth*ty),depth]:[Infinity,Infinity,depth];
      assert.deepEqual(projectPoint(point,pose,aspect),expected);
      const {vertical_correction,...legacy}=pose;
      assert.deepEqual(projectPoint(point,legacy,aspect),expected);
    }
  }
});
