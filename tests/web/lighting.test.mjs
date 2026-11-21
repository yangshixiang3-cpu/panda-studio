import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {lightingText,normalizeLighting,mergedLighting,effectiveLighting,lightingActive,diskAzimuth,diskPoint,previewLightingSettings,
  lightingResponse,DEFAULT_LIGHTING_TUNING,previewFinishTint,previewKeyIrradiance} from '../../web/lighting.js';

const cases=JSON.parse(fs.readFileSync(new URL('../fixtures/lighting-cases.json',import.meta.url),'utf8'));
for(const [i,c] of cases.entries())test(`打光描述共用样例 ${i+1}`,()=>assert.equal(lightingText({...c.lighting,enabled:true}),c.text));
test('正面在下方，屏幕坐标对应 PF1 方位',()=>{
  for(const [x,y,az] of [[90,162,0],[162,90,90],[18,90,-90],[18,162,-45],[162,162,45],[90,18,180]])assert.equal(diskAzimuth(x,y),az);
  for(const az of [-180,-135,-90,-45,0,45,90,135,180])assert.ok(Math.abs(diskAzimuth(...diskPoint(az))-az)<1e-10);
});
test('数据截断与单张整套覆盖',()=>{
  assert.deepEqual(normalizeLighting({azimuth_deg:Infinity,elevation_deg:-5,intensity:8,contrast:-2}),{enabled:false,azimuth_deg:null,elevation_deg:5,intensity:1.5,contrast:0});
  const task={lighting:{azimuth_deg:-45,intensity:1.5}};
  assert.equal(mergedLighting(task,{lighting:null}).azimuth_deg,-45);
  assert.deepEqual(mergedLighting(task,{lighting:{azimuth_deg:45}}),{enabled:false,azimuth_deg:45,elevation_deg:35,intensity:1,contrast:.5});
});
test('旧方向默认关闭，单张关闭时覆盖产品光，零度方向可以生效',()=>{
  assert.equal(lightingText({azimuth_deg:-45}),'模板默认灯光');
  const task={lighting:{enabled:true,azimuth_deg:0}};
  const item={lighting:{enabled:false,azimuth_deg:45}};
  assert.equal(lightingActive(mergedLighting(task,item)),false);
  assert.equal(effectiveLighting(task,item).azimuth_deg,null);
  assert.equal(lightingActive(mergedLighting(task,{lighting:null})),true);
  assert.equal(effectiveLighting(task,{lighting:null}).azimuth_deg,0);
});

test('没有任务调参时与执行器使用同样的默认曲线',()=>{
  assert.deepEqual(previewLightingSettings({}),DEFAULT_LIGHTING_TUNING);
  for(const contrast of [0,.23,.5,1]){
    const ratios=lightingResponse({contrast,intensity:1.2});
    const fill=1-.90*contrast;
    assert.equal(ratios.fill_ratio,fill);
    assert.equal(ratios.bg_fill_ratio,fill);
    assert.equal(ratios.env_gi,1-.85*contrast);
    assert.ok(Math.abs(ratios.key_size-(.50-.25*contrast))<1e-10);
    assert.equal(ratios.key_distance,2.5);
    assert.equal(ratios.key_strength,1.2*(1+111*(1-fill)));
  }
});

test('任务快照驱动补光、GI、主光与背景补光曲线',()=>{
  const task={key_light_intensity:12,ai_calibration:{accepted_overrides:{lights:{intensity_mul:1.1}}},
    lighting_tuning:{fill_min:.1,fill_gamma:2,env_gi_min:.2,key_size_min:.2,key_size_max:.6,
      key_distance:1.8,key_intensity:14,key_compensation:.5,bg_fill_keep:.6}};
  const close=(actual,expected)=>assert.ok(Math.abs(actual-expected)<1e-10);
  for(const [contrast,fill,size,env] of [[0,1,.6,1],[.5,.775,.4,.6],[1,.1,.2,.2]]){
    const ratios=lightingResponse({contrast,intensity:1.2},previewLightingSettings(task));
    close(ratios.fill_ratio,fill);
    close(ratios.bg_fill_ratio,fill+(1-fill)*.6);
    close(ratios.env_gi,env);
    close(ratios.key_size,size);
    assert.equal(ratios.key_distance,1.8);
    close(ratios.key_strength,14*1.2*1.1*(1+.5*(1-fill)));
  }
});

test('网页接受热下发基础强度并忽略无效调参字段',()=>{
  const settings=previewLightingSettings({key_light_intensity:12,lighting_tuning:{
    fill_min:true,fill_gamma:0,key_distance:'2.5',key_size_min:1,key_size_max:.6,
    key_intensity:Infinity,env_gi_min:-1,bg_fill_keep:2,key_compensation:-1}});
  assert.deepEqual(settings,{...DEFAULT_LIGHTING_TUNING,key_intensity:12});
});

test('预览照度跟随面光的角面积，只有明确黑色的饰面使用底色近似',()=>{
  const reference={key_size:.5,key_distance:2.5,key_strength:20};
  assert.equal(previewKeyIrradiance(reference),3);
  assert.equal(previewKeyIrradiance({...reference,key_size:.25}),.75);
  assert.equal(previewKeyIrradiance({...reference,key_distance:5}),.75);
  assert.equal(previewFinishTint('柜体_纯黑台面'),0x606060);
  assert.equal(previewFinishTint('drawer matte black face'),0x606060);
  assert.equal(previewFinishTint('木饰面台面'),null);
});
