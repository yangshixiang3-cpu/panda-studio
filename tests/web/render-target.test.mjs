import assert from 'node:assert/strict';
import test from 'node:test';
import {chipText,chipState,nodeBadge,validCode,nodeKey} from '../../web/render-target.js';

const remote=(target,extra={})=>({online:false,busy:false,max_running:false,target:{host:'10.0.0.5',port:8775,name:'RENDER-01',reachable:true,paired:true,queue:0,...target},...extra});

test('本机 Max 的状态文字和以前一样',()=>{
  assert.equal(chipText({online:true,target:null}),'Max');
  assert.equal(chipText({online:false,max_running:true,target:null}),'Max 已开 · 未连接');
  assert.equal(chipText({online:false,max_running:false,target:null}),'Max 未连接');
  assert.equal(chipText(null),'Max 状态未知');
  assert.equal(chipText({online:false},true),'正在启动 Max…');
  assert.equal(chipState({online:true,busy:true}),'busy');
  assert.equal(chipState(null),'unknown');
});

test('渲染机：连接中、连不上、要连接码、Max 未打开、忙、空闲',()=>{
  assert.equal(chipText(remote({reachable:false,error:'正在连接渲染机…'})),'渲染机 RENDER-01 · 连接中');
  assert.equal(chipState(remote({reachable:false,error:'正在连接渲染机…'})),'unknown');
  assert.equal(chipText(remote({reachable:false,error:'连不上'})),'渲染机 RENDER-01 · 连不上');
  assert.equal(chipState(remote({reachable:false,error:'连不上'})),'offline');
  assert.equal(chipText(remote({paired:false})),'渲染机 RENDER-01 · 要连接码');
  assert.equal(chipText(remote({})),'渲染机 RENDER-01 · Max 未打开');
  assert.equal(chipText(remote({},{online:true,busy:true})),'渲染机 RENDER-01 · 忙');
  assert.equal(chipText(remote({},{online:true})),'渲染机 RENDER-01');
});

test('渲染机列表的小标签',()=>{
  const node={reachable:true,paired:true,online:true,busy:false,queue:0,template:true,max_running:true};
  assert.deepEqual(nodeBadge(node),{text:'空闲',tone:'ok'});
  assert.deepEqual(nodeBadge({...node,busy:true,queue:2}),{text:'忙 · 2 个任务',tone:'warn'});
  assert.deepEqual(nodeBadge({...node,online:false}),{text:'Max 已开 · 未连接',tone:'muted'});
  assert.deepEqual(nodeBadge({...node,online:false,max_running:false}),{text:'Max 未打开',tone:'muted'});
  assert.deepEqual(nodeBadge({...node,paired:false}),{text:'要连接码',tone:'muted'});
  assert.deepEqual(nodeBadge({...node,template:false}),{text:'缺白底模板',tone:'bad'});
  assert.deepEqual(nodeBadge({...node,reachable:false}),{text:'连不上',tone:'bad'});
});

test('连接码和渲染机标识',()=>{
  assert.ok(validCode('012345'));assert.ok(validCode(' 123456 '));
  assert.ok(!validCode('12345'));assert.ok(!validCode('12a456'));assert.ok(!validCode(null));
  assert.equal(nodeKey({host:'pc1',port:8775}),'pc1:8775');
});
