const byId = id => document.getElementById(id);
const start = byId('start-page');
const task = byId('task-page');
const refs = byId('refs-page');
const stage = byId('hero-stage');
const ambient = byId('ambient-particles');
// 动效由程序内开关决定（默认开），不跟随系统「减少动画」：
// 很多电脑关了 Windows「动画效果」，跟随系统会让界面完全没有动效。
// motion.matches 为 true 表示「已关闭动效」，与原 matchMedia 写法语义一致。
const root = document.documentElement;
const motionListeners = new Set();
const motion = {
  get matches() { return root.dataset.motion === 'off'; },
  addEventListener(_type, fn) { motionListeners.add(fn); },
};
if (!root.dataset.motion) root.dataset.motion = 'on';
function syncMotionToggles() {
  const on = !motion.matches;
  document.querySelectorAll('[data-motion-toggle]').forEach(button => {
    button.setAttribute('aria-pressed', String(on));
    button.title = on ? '关闭界面动效（电脑较慢时可关）' : '打开界面动效';
    const state = button.querySelector('.motion-state');
    if (state) state.textContent = on ? '开' : '关';
  });
}
document.addEventListener('click', event => {
  if (!event.target.closest('[data-motion-toggle]')) return;
  const on = motion.matches;
  root.dataset.motion = on ? 'on' : 'off';
  try { localStorage.setItem('whitebg.motion', on ? 'on' : 'off'); } catch { /* 无痕模式等情况下只本次生效 */ }
  syncMotionToggles();
  motionListeners.forEach(fn => fn());
});
syncMotionToggles();
const pageTimers = new Map();
const frame = byId('frame');
const loader = byId('loading-model');

function clearEntry(page) {
  clearTimeout(pageTimers.get(page));
  pageTimers.delete(page);
  page.classList.remove('is-entering');
}

function enter(page) {
  clearEntry(page);
  if (!motion.matches && !document.hidden) {
    page.classList.add('is-entering');
    // 最后一个区块延迟 300ms，动画本身为 480ms。
    pageTimers.set(page, setTimeout(() => clearEntry(page), 800));
  }
}

const pageObserver = new MutationObserver(records => {
  for (const page of new Set(records.map(record => record.target))) {
    if (page.hidden) clearEntry(page);
    else enter(page);
  }
  syncCanvases();
});
for (const page of [start, task, refs]) pageObserver.observe(page, { attributes: true, attributeFilter: ['hidden'] });
if (!location.hash && !start.hidden) enter(start);

let wasLoading = !loader.hidden;
new MutationObserver(() => {
  const ready = loader.hidden;
  if (!ready) frame.classList.remove('reveal');
  else if (wasLoading && !task.hidden && !motion.matches) frame.classList.add('reveal');
  wasLoading = !ready;
}).observe(loader, { attributes: true, attributeFilter: ['hidden'] });
frame.addEventListener('animationend', event => {
  if (event.animationName === 'frame-sweep') frame.classList.remove('reveal');
});

function observeText(id, update) {
  const element = byId(id);
  update(element);
  new MutationObserver(() => update(element)).observe(element, { childList: true, characterData: true, subtree: true });
}

observeText('save-state', element => {
  const text = element.textContent;
  element.dataset.state = text.includes('失败') ? 'failed' : text.includes('正在') ? 'saving' : 'saved';
});

observeText('selected-label', element => {
  const first = element.firstChild;
  if (first?.nodeType !== Node.TEXT_NODE) return;
  const match = /^(\d+)(　.*)$/.exec(first.data);
  if (!match) return;
  const number = document.createElement('span');
  number.className = 'selected-no';
  number.textContent = match[1];
  first.replaceWith(number, match[2]);
});

observeText('ai-state', element => {
  const first = element.firstChild;
  if (first?.nodeType !== Node.TEXT_NODE || !first.data) return;
  const text = document.createElement('span');
  text.className = 'ai-state-text';
  text.textContent = first.data;
  first.replaceWith(text);
});

const dialogContent = byId('dialog-content');
new MutationObserver(() => {
  for (const cell of dialogContent.querySelectorAll('.delivery-table td:is(.new,.archive)')) {
    if (cell.firstElementChild) continue;
    const badge = document.createElement('span');
    badge.className = 'delivery-badge';
    badge.textContent = cell.textContent;
    cell.replaceChildren(badge);
  }
}).observe(dialogContent, { childList: true });

function paintRange(input) {
  const min = input.min === '' ? 0 : Number(input.min);
  const max = input.max === '' ? 100 : Number(input.max);
  const width = max > min ? 100 * (input.valueAsNumber - min) / (max - min) : 0;
  const fill = Math.max(0, Math.min(100, Number.isFinite(width) ? width : 0)).toFixed(2) + '%';
  if (input.style.getPropertyValue('--range-fill') !== fill) input.style.setProperty('--range-fill', fill);
}

// 保留原生赋值及事件语义，只在程序写入滑杆后重画填充。
const inputPrototype = HTMLInputElement.prototype;
const valueDescriptor = Object.getOwnPropertyDescriptor(inputPrototype, 'value');
Object.defineProperty(inputPrototype, 'value', {
  ...valueDescriptor,
  set(value) {
    valueDescriptor.set.call(this, value);
    if (this.type === 'range') paintRange(this);
  }
});
task.addEventListener('input', event => {
  if (event.target instanceof HTMLInputElement && event.target.type === 'range') paintRange(event.target);
}, { capture: true });

function paintRanges(root) {
  for (const input of root.querySelectorAll('input[type=range]')) paintRange(input);
}
paintRanges(task);
for (const id of ['pose-controls', 'parts-controls']) {
  const controls = byId(id);
  new MutationObserver(() => paintRanges(controls)).observe(controls, { childList: true });
}

const sun = byId('lighting-sun');
const sunHalo = byId('lighting-sun-halo');
const beam = byId('lighting-beam');
const beamFill = byId('lighting-beam-fill');
function syncBeam() {
  const x = Number(sun.getAttribute('cx'));
  const y = Number(sun.getAttribute('cy'));
  const length = Math.hypot(90 - x, 90 - y) || 1;
  const nx = (y - 90) / length;
  const ny = (90 - x) / length;
  sunHalo.setAttribute('cx', x);
  sunHalo.setAttribute('cy', y);
  beamFill.setAttribute('x1', x);
  beamFill.setAttribute('y1', y);
  beam.setAttribute('d', [
    ['M', x + nx * 4, y + ny * 4],
    ['L', 90 + nx * 25, 90 + ny * 25],
    ['L', 90 - nx * 25, 90 - ny * 25],
    ['L', x - nx * 4, y - ny * 4], ['Z']
  ].map(point => point.join(' ')).join(' '));
}
syncBeam();
new MutationObserver(syncBeam).observe(sun, { attributes: true, attributeFilter: ['cx', 'cy'] });

const stageContext = stage.getContext('2d');
const ambientContext = ambient.getContext('2d');
const points = Array.from({ length: 72 }, () => ({
  x: Math.random(),
  y: Math.random(),
  angle: Math.random() * Math.PI * 2,
  speed: 2 + Math.random() * 5,
  radius: .45 + Math.random() * .8
}));
const ambientSize = { width: 0, height: 0 };
let animationId = null;
let animationTimer = null;
let lastPaint = null;
let elapsed = 0;
const paintInterval = 1000 / 30;

function resizeSurface(canvas, context) {
  const { width, height } = canvas.getBoundingClientRect();
  const ratio = Math.min(devicePixelRatio || 1, 2);
  const pixelsWide = Math.round(width * ratio);
  const pixelsHigh = Math.round(height * ratio);
  if (canvas.width !== pixelsWide || canvas.height !== pixelsHigh) {
    canvas.width = pixelsWide;
    canvas.height = pixelsHigh;
  }
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { width, height };
}

const tau = Math.PI * 2;
const cabinet = [
  [-.60,0,-.20],[.60,0,-.20],[.60,.63,-.20],[-.60,.63,-.20],
  [-.60,0,.20],[.60,0,.20],[.60,.63,.20],[-.60,.63,.20]
];
const edges = [[0,1],[1,2],[2,3],[3,0],[4,5],[5,6],[6,7],[7,4],[0,4],[1,5],[2,6],[3,7]];
const cabinetDetails = [
  [[-.60,.47,.2],[.60,.47,.2]], [[0,.02,.2],[0,.47,.2]],
  [[-.055,.22,.205],[-.055,.31,.205]], [[.055,.22,.205],[.055,.31,.205]],
  [[-.11,.55,.205],[.11,.55,.205]]
];

function line(ctx, start, end) {
  ctx.moveTo(start[0], start[1]);
  ctx.lineTo(end[0], end[1]);
}

function drawStage(time) {
  const ctx = stageContext;
  if (!ctx || start.hidden) return;
  const { width, height } = resizeSurface(stage, ctx);
  if (!width || !height) return;
  ctx.clearRect(0, 0, width, height);
  const center = width / 2;
  const horizon = height * .68;
  const scale = Math.min(width / 2.02, height / 1.04);
  const pitch = 12 * Math.PI / 180;
  const angle = 35 * Math.PI / 180 * Math.sin(tau * time / 12 + .65);
  const cameraY = .55;

  function project([x, y, z]) {
    const horizontal = x * Math.cos(angle) + z * Math.sin(angle);
    const depth = z * Math.cos(angle) - x * Math.sin(angle);
    const vertical = (y - cameraY) * Math.cos(pitch) - depth * Math.sin(pitch);
    const perspective = 2.9 / (2.9 - depth * Math.cos(pitch) - (y - cameraY) * Math.sin(pitch));
    return [center + horizontal * scale * perspective, height * .465 - (vertical + .255) * scale * perspective];
  }

  ctx.save();
  ctx.beginPath();
  ctx.rect(12, 12, width - 24, height - 24);
  ctx.clip();
  const floor = ctx.createLinearGradient(0, horizon, 0, height);
  floor.addColorStop(0, '#38e1ff03');
  floor.addColorStop(.25, '#38e1ff26');
  floor.addColorStop(1, '#38e1ff12');
  ctx.strokeStyle = floor;
  ctx.lineWidth = .75;
  ctx.beginPath();
  for (let column = -12; column <= 12; column++) {
    line(ctx, [center + column * 12, horizon], [center + column * width * .09, height]);
  }
  for (let row = 1; row < 10; row++) {
    const y = horizon + (height - horizon) * (row / 9) ** 2;
    line(ctx, [0, y], [width, y]);
  }
  ctx.stroke();
  const skyline = ctx.createLinearGradient(0, 0, width, 0);
  skyline.addColorStop(0, 'transparent');
  skyline.addColorStop(.5, '#5eeeff85');
  skyline.addColorStop(1, 'transparent');
  ctx.beginPath();
  ctx.strokeStyle = skyline;
  ctx.shadowColor = '#38e1ff';
  ctx.shadowBlur = 10;
  line(ctx, [13, horizon], [width - 13, horizon]);
  ctx.stroke();

  const vertices = cabinet.map(project);
  const segments = [...edges.map(([a, b]) => [vertices[a], vertices[b]]), ...cabinetDetails.map(detail => detail.map(project))];
  // 淡青玻璃面为线框提供层次，线段在扫描带内再次提亮。
  ctx.shadowBlur = 0;
  for (const [face, fill] of [
    [[3,2,6,7],'#38e1ff0d'], [[1,2,6,5],'#4d7cff13'], [[4,5,6,7],'#38e1ff15']
  ]) {
    ctx.beginPath();
    face.forEach((index, point) => ctx[point === 0 ? 'moveTo' : 'lineTo'](...vertices[index]));
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
  }
  function traceCabinet(color, blur) {
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.05;
    ctx.shadowBlur = blur;
    for (const [a, b] of segments) line(ctx, a, b);
    ctx.stroke();
  }
  traceCabinet('#65dcf6ad', 7);
  const scanY = height * (.22 + .52 * ((time + 1.15) % 4) / 4);
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, scanY - 4, width, 9);
  ctx.clip();
  traceCabinet('#b6faff', 13);
  ctx.restore();
  ctx.strokeStyle = '#73efff82';
  ctx.shadowBlur = 12;
  ctx.lineWidth = .85;
  ctx.beginPath();
  line(ctx, [width * .125, scanY], [width * .875, scanY]);
  ctx.stroke();
  ctx.restore();

  ctx.strokeStyle = '#6ceaffb5';
  ctx.lineWidth = 1.15;
  ctx.shadowColor = '#38e1ff';
  ctx.shadowBlur = 7;
  ctx.beginPath();
  for (const [x, y, dx, dy] of [[12,12,1,1],[width-12,12,-1,1],[12,height-12,1,-1],[width-12,height-12,-1,-1]]) {
    ctx.moveTo(x + dx * 15, y);
    ctx.lineTo(x, y);
    ctx.lineTo(x, y + dy * 15);
  }
  ctx.stroke();
  ctx.shadowBlur = 0;
}

function drawAmbient(delta) {
  const ctx = ambientContext;
  const { width, height } = ambientSize;
  if (!ctx || !width || !height) return;
  ctx.clearRect(0, 0, width, height);
  for (const point of points) {
    point.x = (point.x + Math.cos(point.angle) * point.speed * delta / width + 1) % 1;
    point.y = (point.y + Math.sin(point.angle) * point.speed * delta / height + 1) % 1;
  }
  for (let i = 0; i < points.length; i++) {
    const point = points[i];
    const x = point.x * width;
    const y = point.y * height;
    ctx.fillStyle = '#91dfff50';
    ctx.beginPath();
    ctx.arc(x, y, point.radius, 0, tau);
    ctx.fill();
    for (let j = i + 1; j < points.length; j++) {
      const other = points[j];
      const otherX = other.x * width;
      const otherY = other.y * height;
      const distance = (x - otherX) ** 2 + (y - otherY) ** 2;
      if (distance >= 10000) continue;
      ctx.strokeStyle = 'rgba(105,190,246,' + (.12 * (1 - distance / 10000)) + ')';
      ctx.lineWidth = .55;
      ctx.beginPath();
      line(ctx, [x, y], [otherX, otherY]);
      ctx.stroke();
    }
  }
}

function canAnimate() {
  return !document.hidden && !motion.matches && task.hidden && (!start.hidden || !refs.hidden);
}

function animate(now) {
  animationId = null;
  if (!canAnimate()) return;
  const delta = lastPaint === null ? paintInterval : now - lastPaint;
  if (delta >= paintInterval - .25) {
    const seconds = Math.min(delta, 80) / 1000;
    lastPaint = now;
    elapsed += seconds;
    drawStage(elapsed);
    drawAmbient(seconds);
  }
  schedulePaint();
}

function schedulePaint() {
  if (animationId !== null || animationTimer !== null) return;
  // 临近下一次绘制时才请求浏览器帧，高刷新率下仍检查帧间隔。
  const delay = lastPaint === null ? 0 : lastPaint + paintInterval - performance.now() - 7;
  if (delay <= 1) {
    animationId = requestAnimationFrame(animate);
    return;
  }
  animationTimer = setTimeout(() => {
    animationTimer = null;
    if (canAnimate()) animationId = requestAnimationFrame(animate);
  }, delay);
}

function syncCanvases() {
  if (!canAnimate()) {
    if (animationId !== null) cancelAnimationFrame(animationId);
    clearTimeout(animationTimer);
    animationId = null;
    animationTimer = null;
    lastPaint = null;
    if (motion.matches && !document.hidden && task.hidden && !start.hidden) drawStage(0);
    return;
  }
  if (ambientContext) Object.assign(ambientSize, resizeSurface(ambient, ambientContext));
  schedulePaint();
}

new ResizeObserver(() => {
  if (motion.matches && !document.hidden && task.hidden && !start.hidden) drawStage(0);
}).observe(stage);
window.addEventListener('resize', syncCanvases);
document.addEventListener('visibilitychange', syncCanvases);
motion.addEventListener('change', () => {
  if (motion.matches) {
    for (const page of pageTimers.keys()) clearEntry(page);
    frame.classList.remove('reveal');
  }
  syncCanvases();
});
syncCanvases();
