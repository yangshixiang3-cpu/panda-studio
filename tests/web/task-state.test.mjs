import assert from 'node:assert/strict';
import test from 'node:test';
import {bundleSignature,pollDelay,rowStatus,taskSteps} from '../../web/task-state.js';
import {syncRange} from '../../web/range.js';

const item=(smallshot,final,enabled=true)=>({enabled,smallshot:{state:smallshot},final:{state:final,file:'final/1.jpg',job_id:'r1'}});
const primary=steps=>steps.filter(step=>step.current).map(step=>step.mode);

test('步骤跟随启用项、小样、成品与本次入库状态',()=>{
  const task={items:[item('none','none'),item('none','none',false)]};
  assert.deepEqual(primary(taskSteps(task)),['smallshot']);
  task.items[0]=item('done','none');
  assert.deepEqual(primary(taskSteps(task)),['final']);
  task.items[0]=item('done','done');
  assert.deepEqual(primary(taskSteps(task)),['delivery']);
  task.delivered={files:['1.jpg'],at:'2026-11-20T13:00:00+09:00'};
  assert.deepEqual(primary(taskSteps(task)),[]);
  assert.equal(taskSteps(task).at(-1).text,'已入库 ✓');
  assert.deepEqual(primary(taskSteps(task,{jobs:[{job_id:'r1',updated_at:'2026-11-20T14:00:00+09:00'}]})),['delivery']);
});

test('进行中步骤使用后端计数与估时，旧回包使用进行中',()=>{
  const task={items:[item('rendering','none')]};
  assert.equal(taskSteps(task,{progress:{kind:'smallshot',done:2,total:6,eta_sec:30}})[0].text,'出小样 2/6 · 约 30 秒');
  assert.equal(taskSteps(task,{progress:{kind:'final',done:1,total:6,eta_sec:480}})[1].text,'渲染 1/6 · 约 8 分钟');
  assert.equal(taskSteps(task)[0].text,'出小样 进行中');
  assert.equal(rowStatus(item('rendering','done')).text,'出小样中');
});

test('任务签名优先，旧回包按 revision 与实际内容去重',()=>{
  const data={task:{revision:1},signature:'a'};
  assert.equal(bundleSignature(data),bundleSignature({...data,revision:2}));
  assert.notEqual(bundleSignature({task:{revision:1}}),bundleSignature({task:{revision:2}}));
  assert.equal(bundleSignature({task:{},revision:2}),bundleSignature({task:{revision:2}}));
  assert.notEqual(bundleSignature({task:{},preview:{state:'queued'}}),bundleSignature({task:{},preview:{state:'done'}}));
  assert.equal(pollDelay({progress:{kind:null},runner:{online:true}}),8000);
  assert.equal(pollDelay({progress:{kind:'calibration'}}),1800);
  assert.equal(pollDelay({runner:{online:false}}),1800);
  assert.equal(pollDelay({jobs:[{state:'queued'}]}),1800);
});

test('滑杆填充对程序与用户更新使用同一比例',()=>{
  const props=new Map(),input={value:'0',min:'-50',max:'50',
    style:{getPropertyValue:key=>props.get(key),setProperty:(key,value)=>props.set(key,value)}};
  syncRange(input);
  assert.equal(props.get('--fill'),'50%');
  syncRange(input,25);
  assert.equal(input.value,25);
  assert.equal(props.get('--fill'),'75%');
  input.value='-50';syncRange(input);
  assert.equal(props.get('--fill'),'0%');
});
