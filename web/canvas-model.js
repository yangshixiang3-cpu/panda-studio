// 画布的纯计算（视角换算、缩放、对齐格栅、节点排布、参考图），网页和 tests/web 共用。

export const RATIOS=['1:1','3:4','4:3','2:3','3:2','9:16','16:9'];
export const SIZES=['1K','2K','4K'];
export const QUALITIES=[['standard','标准画质'],['high','高清画质']];
export const NODE_WIDTH=336;                // 14 个格栅宽：节点左右边都落在格栅点上
export const GRID=24;                       // 画布底纹点的间距；节点位置对齐到这些点
export const GAP=GRID*2;                    // 纵向排列时节点之间的间隔
export const ZOOM_MIN=0.2,ZOOM_MAX=2.5;
export const clampZoom=z=>Math.min(ZOOM_MAX,Math.max(ZOOM_MIN,z));
export const snap=value=>Math.round(value/GRID)*GRID;
export const snapPoint=({x,y})=>({x:snap(x),y:snap(y)});

// 屏幕坐标 → 画布坐标。view={x,y,zoom}，origin 为画布容器左上角的屏幕坐标
export function toWorld(view,origin,clientX,clientY){
  return {x:(clientX-origin.x-view.x)/view.zoom,y:(clientY-origin.y-view.y)/view.zoom};
}

// 以屏幕上某一点为中心缩放：那一点下面的画布内容保持不动
export function zoomAround(view,origin,clientX,clientY,zoom){
  const next=clampZoom(zoom),world=toWorld(view,origin,clientX,clientY);
  return {x:clientX-origin.x-world.x*next,y:clientY-origin.y-world.y*next,zoom:next};
}

// 让所有节点都进画面（留边距，最大放到 100%）；没有节点时回到原点
export function fitView(boxes,width,height,padding=80){
  if(!boxes.length)return {x:width/2-NODE_WIDTH/2,y:height/3,zoom:1};
  const left=Math.min(...boxes.map(b=>b.x)),top=Math.min(...boxes.map(b=>b.y));
  const right=Math.max(...boxes.map(b=>b.x+b.w)),bottom=Math.max(...boxes.map(b=>b.y+b.h));
  const zoom=clampZoom(Math.min(1,(width-padding*2)/Math.max(1,right-left),(height-padding*2)/Math.max(1,bottom-top)));
  return {x:width/2-(left+right)/2*zoom,y:height/2-(top+bottom)/2*zoom,zoom};
}

// 节点卡片（图片区域）的高度：按图片或要生成的比例，太高的限制在 1.4 倍宽，再对齐到格栅
// 卡片里图片区的高度：生图节点按选的比例（换比例时框跟着变，当预览；图片在框里完整显示）；上传的图按图片本身
export function mediaHeight(node,width=NODE_WIDTH){
  const current=node.results?.[node.current??0];
  let ratio;
  if(node.kind!=='gen'&&current?.w&&current?.h)ratio=current.h/current.w;
  else{const [w,h]=(node.gen?.ratio||'16:9').split(':').map(Number);ratio=h/w;}
  const cap=Math.floor(width*1.4/GRID)*GRID;               // 上限也向下取整到格栅，对齐后不会超过
  return Math.min(cap,Math.max(GRID*4,snap(Math.max(width*0.4,width*ratio))));
}

// 某个节点的参考图：连进来的节点各自当前显示的那张图
export function refsOf(id,nodes,edges){
  return edges.filter(e=>String(e.to)===String(id)).map(e=>nodes.get(String(e.from)))
    .map(n=>n?.results?.[n.current??0]).filter(Boolean);
}

const overlaps=(a,b)=>a.x<b.x+b.w&&b.x<a.x+a.w&&a.y<b.y+b.h&&b.y<a.y+a.h;

// 同一个节点一次生成多张：在它下面同一列纵向排开（对齐格栅，被占的位置往下跳）
export function stackBelow(source,heights,boxes,gap=GAP){
  const placed=[],taken=boxes.filter(b=>b.id!==source.id).map(b=>({...b}));
  let y=snap(source.y+source.h+gap);
  for(const h of heights){
    let box={x:snap(source.x),y,w:NODE_WIDTH,h};
    for(let guard=0;guard<200;guard++){
      const hit=taken.find(b=>overlaps(box,b));
      if(!hit)break;
      box={...box,y:snap(hit.y+hit.h+gap)};
    }
    placed.push({x:box.x,y:box.y});taken.push(box);
    y=snap(box.y+h+gap);
  }
  return placed;
}

// 一次放进来多张图：排成一行（对齐格栅，间隔 GAP）
export function rowLayout(start,count,gap=GAP){
  const origin=snapPoint(start);
  return Array.from({length:count},(_,i)=>({x:origin.x+i*(NODE_WIDTH+gap),y:origin.y}));
}

// 新节点放在旁边：右侧，若那里已有节点就往下错开（对齐格栅）
export function besideOf(source,boxes,gap=GAP*2){
  let box={x:snap(source.x+source.w+gap),y:snap(source.y),w:NODE_WIDTH,h:source.h};
  for(let i=0;i<50;i++){
    const hit=boxes.find(b=>b.id!==source.id&&overlaps(box,b));
    if(!hit)break;
    box={...box,y:snap(hit.y+hit.h+GAP)};
  }
  return {x:box.x,y:box.y};
}

// 生成参数的一行摘要：16:9 · 标准画质 · 2K · 1张
export function paramsText(gen){
  const quality=QUALITIES.find(([key])=>key===gen.quality)?.[1]||'标准画质';
  return `${gen.ratio||'16:9'} · ${quality} · ${gen.resolution||'2K'} · ${gen.count||1}张`;
}

export const nodeName=seq=>`图片 ${seq}`;
export const runTime=seconds=>{const s=Math.max(0,Math.floor(seconds));return `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;};

// ── 对齐参考线（像 PS / PPT）：拖动时，被拖的那块的左边、中线、右边（上、中、下）
// 靠近别的节点的同类位置就吸过去，并给出要画的虚线 ──
export const ALIGN_THRESHOLD=6;   // 屏幕像素；用的时候除以缩放
function nearest(values,targets,threshold){
  let best=null;
  for(const v of values)for(const t of targets){
    const d=t.at-v;
    if(Math.abs(d)<=threshold&&(!best||Math.abs(d)<Math.abs(best.d)))best={d,at:t.at};
  }
  return best;
}
export function alignGuides(moving,others,threshold){
  const xs=b=>[b.x,b.x+b.w/2,b.x+b.w],ys=b=>[b.y,b.y+b.h/2,b.y+b.h];
  const tx=others.flatMap(b=>xs(b).map(at=>({at,box:b}))),ty=others.flatMap(b=>ys(b).map(at=>({at,box:b})));
  const bx=nearest(xs(moving),tx,threshold),by=nearest(ys(moving),ty,threshold);
  const dx=bx?bx.d:0,dy=by?by.d:0;
  const moved={...moving,x:moving.x+dx,y:moving.y+dy};
  const lines=[];
  if(bx){
    const hits=tx.filter(t=>xs(moved).some(v=>Math.abs(v-t.at)<0.5));
    for(const at of new Set(hits.map(h=>h.at))){
      const boxes=[moved,...hits.filter(h=>h.at===at).map(h=>h.box)];
      lines.push({axis:'x',at,from:Math.min(...boxes.map(b=>b.y)),to:Math.max(...boxes.map(b=>b.y+b.h))});
    }
  }
  if(by){
    const hits=ty.filter(t=>ys(moved).some(v=>Math.abs(v-t.at)<0.5));
    for(const at of new Set(hits.map(h=>h.at))){
      const boxes=[moved,...hits.filter(h=>h.at===at).map(h=>h.box)];
      lines.push({axis:'y',at,from:Math.min(...boxes.map(b=>b.x)),to:Math.max(...boxes.map(b=>b.x+b.w))});
    }
  }
  return {dx,dy,snappedX:!!bx,snappedY:!!by,lines};
}

// 几块的外框
export function boundsOf(boxes){
  const left=Math.min(...boxes.map(b=>b.x)),top=Math.min(...boxes.map(b=>b.y));
  return {x:left,y:top,w:Math.max(...boxes.map(b=>b.x+b.w))-left,h:Math.max(...boxes.map(b=>b.y+b.h))-top};
}
export const intersects=(a,b)=>a.x<b.x+b.w&&b.x<a.x+a.w&&a.y<b.y+b.h&&b.y<a.y+a.h;

// 框选：拖出来的矩形（两个角，任意方向）碰到的节点
export function marqueeHits(start,end,boxes){
  const rect={x:Math.min(start.x,end.x),y:Math.min(start.y,end.y),w:Math.abs(end.x-start.x),h:Math.abs(end.y-start.y)};
  return boxes.filter(b=>intersects(rect,b)).map(b=>b.id);
}

// 分组框：成员外框四周留 GRID，上面再留出放名字的地方（节点名字在卡片上方，也要包进去）
export const GROUP_PAD=GRID,GROUP_LABEL=GRID*2;
export function groupFrame(boxes){
  const b=boundsOf(boxes);
  return {x:b.x-GROUP_PAD,y:b.y-GROUP_PAD-GROUP_LABEL,w:b.w+GROUP_PAD*2,h:b.h+GROUP_PAD*2+GROUP_LABEL};
}

// 连线的曲线（和 Drawflow 的连线一样：水平方向的三次贝塞尔）
export function curvePath(x1,y1,x2,y2,curvature=0.45){
  const hx=Math.abs(x2-x1)*curvature;
  return `M ${x1} ${y1} C ${x1+hx} ${y1} ${x2-hx} ${y2} ${x2} ${y2}`;
}

// 提示词里的 @图片名 → 按参考图顺序换成「图1」「图2」……（接口只收到图片顺序，不知道画布上的名字）。
// refs：按发送顺序的参考图 [{name}]。名字里的空格可有可无（@图片 3 和 @图片3 都认），@图片 3 不会吃掉 @图片 31。
export function resolveMentions(prompt,refs){
  let text=String(prompt||''),used=false;
  refs.forEach((ref,i)=>{
    const name=String(ref.name||'').trim();if(!name)return;
    const body=name.split(/\s+/).map(part=>part.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('\\s*');
    text=text.replace(new RegExp('@'+body+'(?![0-9])','g'),()=>{used=true;return `图${i+1}`;});
  });
  if(used)text=(refs.length>1?`参考图按顺序编号：图1～图${refs.length}。`:'参考图是图1。')+'\n'+text;
  return {text,used};
}

// 生图进度（估的：接口不报进度）。expect 是这个渠道平时出一张图要的秒数：
// 到 expect 时约 85%，之后越走越慢，停在 95% 等结果；数字只会往上走，不会跳回去
export function progressAt(seconds,expect=45){
  const t=Math.max(0,Number(seconds)||0),e=Math.max(5,Number(expect)||45);
  return Math.min(95,Math.floor(95*(1-Math.exp(-2.25*t/e))));
}

// 脑图式排布：在 source 右边分出一列节点（heights 是每个节点的卡片高度），整列上下居中对齐 source，都落在格栅点上；
// 那一列已经有节点挡着（比如之前分出来的），就整列往下挪到它们下面，不重叠
export function branchLayout(source,heights,boxes,gapX=GRID*5,gapY=GAP){
  const x=snap(source.x+source.w+gapX);
  const total=heights.reduce((sum,h)=>sum+h,0)+gapY*Math.max(0,heights.length-1);
  let top=snap(source.y+source.h/2-total/2);
  const others=boxes.filter(b=>b.id!==source.id);
  for(let guard=0;guard<200;guard++){
    const block={x,y:top,w:NODE_WIDTH,h:total};
    const hits=others.filter(b=>overlaps(block,b));
    if(!hits.length)break;
    top=snap(Math.max(...hits.map(b=>b.y+b.h))+gapY);
  }
  const spots=[];let y=top;
  for(const h of heights){spots.push({x,y});y=snap(y+h+gapY);}
  return spots;
}

export const MAX_CANVAS_POLL_FAILURES=5;

// 多节点生图轮询间隔与异常退避：节点较多时适当合并节流，连续失败时指数退避（封顶 15 秒）
export function canvasPollDelay(runningCount,failures=0){
  if(!(runningCount>0))return 0;
  if(failures>0)return Math.min(15000,2000*2**(failures-1));
  return runningCount<=2?1500:Math.min(3000,1500+(runningCount-2)*400);
}
