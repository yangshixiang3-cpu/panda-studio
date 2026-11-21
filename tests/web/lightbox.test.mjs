import test from 'node:test';
import assert from 'node:assert/strict';
import {clampScale,zoomAt,pageIndex} from '../../web/lightbox.js';

test('缩放夹取在适应窗口到八倍原图之间',()=>{
  assert.equal(clampScale(.01,.25),.25);
  assert.equal(clampScale(20,.25),8);
  assert.equal(clampScale(2,.25),2);
});
test('光标中心缩放保持光标下的像素不动',()=>{
  const pose={scale:.5,x:20,y:-30},point={x:130,y:90};
  const result=zoomAt(pose,2,point);
  assert.deepEqual(result,{scale:2,x:-310,y:-390});
  for(const axis of ['x','y'])assert.equal((point[axis]-pose[axis])/pose.scale,(point[axis]-result[axis])/result.scale);
  assert.deepEqual(zoomAt(result,.5,point),pose);
});
test('中心缩放保持居中，负向平移同样按比例变化',()=>{
  assert.deepEqual(zoomAt({scale:1,x:-20,y:40},2,{x:0,y:0}),{scale:2,x:-40,y:80});
});
test('翻页在首尾停止，空组和单张不越界',()=>{
  assert.equal(pageIndex(0,-1,3),0);assert.equal(pageIndex(2,1,3),2);
  assert.equal(pageIndex(0,1,3),1);assert.equal(pageIndex(2,-1,3),1);
  assert.equal(pageIndex(0,1,1),0);assert.equal(pageIndex(0,-1,0),0);
});
