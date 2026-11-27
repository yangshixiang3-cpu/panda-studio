import assert from 'node:assert/strict';
import test from 'node:test';
import {toWorld,zoomAround,fitView,mediaHeight,refsOf,besideOf,rowLayout,stackBelow,snap,snapPoint,paramsText,clampZoom,runTime,
  NODE_WIDTH,GRID,GAP,ZOOM_MIN,ZOOM_MAX} from '../../web/canvas-model.js';
import {appendPrompt,BUILTIN_PRESETS} from '../../web/canvas-presets.js';

const close=(a,b)=>assert.ok(Math.abs(a-b)<1e-9,`${a} ≈ ${b}`);
const onGrid=value=>value%GRID===0;

test('以鼠标为中心缩放：鼠标下面的画布内容不动',()=>{
  const view={x:120,y:-40,zoom:0.8},origin={x:76,y:12};
  const before=toWorld(view,origin,500,300);
  const next=zoomAround(view,origin,500,300,1.7);
  const after=toWorld(next,origin,500,300);
  close(before.x,after.x);close(before.y,after.y);
  assert.equal(next.zoom,1.7);
  assert.equal(zoomAround(view,origin,0,0,99).zoom,ZOOM_MAX);
  assert.equal(clampZoom(0.01),ZOOM_MIN);
});

test('适应画面：所有节点都在可视区域里，最多放到 100%',()=>{
  const boxes=[{x:0,y:0,w:320,h:400},{x:900,y:500,w:320,h:300}];
  const view=fitView(boxes,1200,800);
  assert.ok(view.zoom<=1&&view.zoom>0);
  for(const b of boxes){
    const left=view.x+b.x*view.zoom,right=view.x+(b.x+b.w)*view.zoom,top=view.y+b.y*view.zoom,bottom=view.y+(b.y+b.h)*view.zoom;
    assert.ok(left>=0&&right<=1200&&top>=0&&bottom<=800);
  }
  assert.equal(fitView([],1000,600).zoom,1);
});

test('对齐格栅：位置和卡片高度都落在格栅点上',()=>{
  assert.equal(snap(37),48);assert.equal(snap(-13),-24);assert.equal(snap(11),0);
  assert.deepEqual(snapPoint({x:130,y:59}),{x:120,y:48});
  for(const node of [{gen:{ratio:'16:9'}},{gen:{ratio:'3:4'}},{results:[{w:1000,h:5000}]},{results:[{w:1600,h:900}]}])
    assert.ok(onGrid(mediaHeight(node)),JSON.stringify(node));
  assert.ok(mediaHeight({results:[{w:1000,h:5000}]})<=NODE_WIDTH*1.4);
  assert.ok(Math.abs(mediaHeight({gen:{ratio:'16:9'}})-NODE_WIDTH*9/16)<=GRID/2);
});

test('一个节点生成多张：在下面同一列纵向排开，不重叠、对齐格栅，被占的位置往下跳',()=>{
  const source={id:'1',x:96,y:48,w:NODE_WIDTH,h:192};
  assert.ok(onGrid(NODE_WIDTH));
  const spots=stackBelow(source,[192,192],[source]);
  assert.deepEqual(spots.map(s=>s.x),[96,96]);
  assert.equal(spots[0].y,48+192+GAP);
  assert.equal(spots[1].y,spots[0].y+192+GAP);
  assert.ok(spots.every(s=>onGrid(s.x)&&onGrid(s.y)));
  const blocker={id:'9',x:96,y:288,w:NODE_WIDTH,h:96};
  const moved=stackBelow(source,[192],[source,blocker]);
  assert.equal(moved[0].y,288+96+GAP);
});

test('参考图：连进来的节点当前显示的那张',()=>{
  const nodes=new Map([['1',{results:[{file:'a.png'},{file:'b.png'}],current:1}],['2',{results:[]}],['3',{results:[{file:'c.png'}]}]]);
  const edges=[{from:'1',to:'5'},{from:'2',to:'5'},{from:'3',to:'5'},{from:'3',to:'6'}];
  assert.deepEqual(refsOf('5',nodes,edges).map(r=>r.file),['b.png','c.png']);
  assert.deepEqual(refsOf(6,nodes,edges).map(r=>r.file),['c.png']);
});

test('继续生成放右边、多张上传排一行，都对齐格栅',()=>{
  const source={id:'1',x:0,y:0,w:NODE_WIDTH,h:192};
  const right=besideOf(source,[source]);
  assert.ok(right.x>source.x+source.w&&onGrid(right.x)&&onGrid(right.y));
  const blocked=besideOf(source,[source,{id:'2',x:right.x,y:0,w:NODE_WIDTH,h:192}]);
  assert.ok(blocked.y>=192);
  const row=rowLayout({x:13,y:29},3);
  assert.ok(row.every(s=>onGrid(s.x)&&s.y===row[0].y));
  assert.ok(row[1].x-row[0].x>NODE_WIDTH);
});

test('参数摘要和预设提示词',()=>{
  assert.equal(paramsText({ratio:'16:9',quality:'standard',resolution:'2K',count:1}),'16:9 · 标准画质 · 2K · 1张');
  assert.equal(paramsText({ratio:'3:4',quality:'high',resolution:'4K',count:3}),'3:4 · 高清画质 · 4K · 3张');
  assert.equal(appendPrompt('','北欧原木风'),'北欧原木风');
  assert.equal(appendPrompt('白底主图。','北欧原木风'),'白底主图，北欧原木风');
  assert.equal(appendPrompt('白底主图，北欧原木风','北欧原木风'),'白底主图，北欧原木风');
  assert.ok(BUILTIN_PRESETS.length>=4&&BUILTIN_PRESETS.every(t=>typeof t==='string'&&t.length<=500));
});

test('生成计时',()=>{
  assert.equal(runTime(0),'00:00');assert.equal(runTime(75.9),'01:15');assert.equal(runTime(-3),'00:00');
});

import {alignGuides,boundsOf,marqueeHits,groupFrame,GROUP_PAD,GROUP_LABEL} from '../../web/canvas-model.js';

test('对齐参考线：左边、中线、底边靠近就吸过去，并给出虚线',()=>{
  const other={x:0,y:0,w:336,h:192};
  const left=alignGuides({x:4,y:400,w:336,h:192},[other],6);
  assert.equal(left.dx,-4);assert.equal(left.dy,0);assert.ok(left.snappedX&&!left.snappedY);
  assert.deepEqual(left.lines.filter(l=>l.axis==='x').map(l=>l.at).sort((a,b)=>a-b),[0,168,336]);   // 宽度相同：左中右都对上
  assert.deepEqual(left.lines.find(l=>l.at===0),{axis:'x',at:0,from:0,to:592});
  const center=alignGuides({x:500,y:93,w:336,h:96},[other],6);                // 中线对中线
  assert.equal(center.dy,3);assert.ok(center.snappedY);
  const far=alignGuides({x:520,y:320,w:336,h:96},[other],6);
  assert.deepEqual([far.dx,far.dy,far.lines.length],[0,0,0]);
  const closest=alignGuides({x:333,y:500,w:100,h:50},[other,{x:330,y:0,w:10,h:10}],6);
  assert.equal(closest.dx,2);                                                  // 取最近的那条（335 比 336 近）
});

test('框选和分组框',()=>{
  const boxes=[{id:'1',x:0,y:0,w:336,h:192},{id:'2',x:480,y:0,w:336,h:192},{id:'3',x:0,y:400,w:336,h:192}];
  assert.deepEqual(marqueeHits({x:-20,y:-20},{x:500,y:100},boxes),['1','2']);
  assert.deepEqual(marqueeHits({x:500,y:450},{x:-20,y:300},boxes),['3']);    // 反方向拖也行
  assert.deepEqual(boundsOf(boxes.slice(0,2)),{x:0,y:0,w:816,h:192});
  assert.deepEqual(groupFrame(boxes.slice(0,2)),{x:-GROUP_PAD,y:-GROUP_PAD-GROUP_LABEL,w:816+GROUP_PAD*2,h:192+GROUP_PAD*2+GROUP_LABEL});
});

import {curvePath,resolveMentions} from '../../web/canvas-model.js';
test('分组连线的曲线',()=>{
  assert.equal(curvePath(0,0,100,50),'M 0 0 C 45 0 55 50 100 50');
});
test('提示词里 @ 引用图片换成参考图编号',()=>{
  const refs=[{name:'图片 3'},{name:'图片 31'},{name:'图片 7'}];
  assert.deepEqual(resolveMentions('把@图片 3的柜子放进@图片7的客啊，颜色参考 @图片31',refs),
    {text:'参考图按顺序编号：图1～图3。\n把图1的柜子放进图3的客啊，颜色参考 图2',used:true});
  assert.deepEqual(resolveMentions('白底主图，@图片 9 不在参考图里',refs),{text:'白底主图，@图片 9 不在参考图里',used:false});
  assert.equal(resolveMentions('参考 @图片 3',[{name:'图片 3'}]).text,'参考图是图1。\n参考 图1');
  assert.equal(resolveMentions('',refs).text,'');
});

import {progressAt} from '../../web/canvas-model.js';
test('生图节点的框按选的比例显示，上传的图按图片本身',()=>{
  const image={file:'a.png',w:1600,h:1200};
  assert.equal(mediaHeight({kind:'gen',gen:{ratio:'1:1'},results:[image]}),336);
  assert.equal(mediaHeight({kind:'gen',gen:{ratio:'16:9'},results:[]}),192);
  assert.equal(mediaHeight({kind:'upload',gen:{ratio:'1:1'},results:[image]}),264);      // 252 对齐格子取 264
});
test('生图进度：一开始走得快，越往后越慢，最多 95%，不会往回跳',()=>{
  assert.equal(progressAt(0,40),0);
  assert.ok(progressAt(4,40)>15&&progressAt(4,40)<25);
  assert.ok(progressAt(40,40)>=80&&progressAt(40,40)<=88);
  assert.equal(progressAt(400,40),94);
  let last=-1;for(let t=0;t<200;t+=0.2){const p=progressAt(t,40);assert.ok(p>=last);last=p;}
});

import {branchLayout} from '../../web/canvas-model.js';
test('脑图排布：右边一列、上下居中对齐原节点、落在格栅上、有挡着的就往下挪',()=>{
  const source={id:'1',x:0,y:0,w:NODE_WIDTH,h:240};
  const spots=branchLayout(source,[240,240,240],[source]);
  assert.equal(new Set(spots.map(s=>s.x)).size,1);
  assert.ok(spots[0].x>=source.x+source.w+GRID*4);
  const top=spots[0].y,bottom=spots[2].y+240;
  assert.ok(Math.abs((top+bottom)/2-(source.y+source.h/2))<=GRID,JSON.stringify(spots));
  assert.ok(spots.every(s=>s.x%GRID===0&&s.y%GRID===0));
  assert.ok(spots[1].y>=spots[0].y+240&&spots[2].y>=spots[1].y+240);
  // 那一列已经有节点：新的一列挪到它们下面
  const taken=spots.map((s,i)=>({id:'c'+i,x:s.x,y:s.y,w:NODE_WIDTH,h:240}));
  const more=branchLayout(source,[240,240],[source,...taken]);
  assert.ok(more[0].y>=bottom,JSON.stringify(more));
});
