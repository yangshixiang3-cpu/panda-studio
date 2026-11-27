const fs=require('node:fs'),path=require('node:path');
const {setTimeout:sleep}=require('node:timers/promises');
const {installProbe}=require('./probe.cjs');
const {outDir,url,withPage,withService,prepare,scan,modelReady,done}=require('./harness.cjs');
const slider='#control-azimuth';
const quantify = (values, q) => values.length ? [...values].sort((a, b) => a - b)[Math.ceil(values.length * q) - 1] : null;
const average = values => values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
const summary = values => ({
  count: values.length, meanMs: average(values), p95Ms: quantify(values, .95),
  p99Ms: quantify(values, .99), maxMs: values.length ? Math.max(...values) : null,
});

async function xy(page, selector, x, y) {
  const box = await page.locator(selector).boundingBox();
  if (!box) throw Error('目标区域不可用 ' + selector);
  return { x: Math.round(box.x + x * box.width), y: Math.round(box.y + y * box.height) };
}
const gestureDuration = 3000;
const inputRate = 60;
async function dispatchTimed(cdp, eventFor) {
  const n = gestureDuration / 1000 * inputRate;
  const schedule = [];
  const sent = [];
  const start = performance.now();
  for (let index = 1; index <= n; index++) {
    const dueMs = (index - 1) * 1000 / inputRate;
    const wait = start + dueMs - performance.now();
    if (wait > 0) await sleep(wait);
    const payload = eventFor(index / n);
    const sample = { index, dueMs, sentMs: performance.now() - start, ...payload };
    schedule.push(sample);
    sent.push(cdp.send('Input.dispatchMouseEvent', payload).then(
      () => { sample.ackMs = performance.now() - start; },
      error => { sample.error = error.message; },
    ));
  }
  const remaining = gestureDuration - (performance.now() - start);
  if (remaining > 0) await sleep(remaining);
  await Promise.all(sent);
  return schedule;
}

function summarizeProbe(raw) {
  const intervals = raw.frames.slice(1).map((stamp, index) => stamp - raw.frames[index]);
  const gaps = summary(intervals);
  const callbacks = raw.animationCallbacks;
  const callbacksInWindow = callbacks.filter(at => at >= raw.startAt && at < raw.endAt).length;
  const callbackIntervals = callbacks.slice(1).map((at, index) => at - callbacks[index]);
  const delays = raw.moves.filter(move => move.nextFrameAt != null).map(move => move.nextFrameAt - move.at);
  const dispatchDelays = raw.moves.filter(move => move.nextFrameAt != null).map(move => move.nextFrameAt - move.deliveredAt);
  return { fps: callbacksInWindow * 1000 / (raw.endAt - raw.startAt),
    fpsFromFrameTimestamps: gaps.meanMs ? 1000 / gaps.meanMs : null, callbacksInWindow,
    callbackIntervalsMs: callbackIntervals, callbackFrame: summary(callbackIntervals),
    intervalsMs: intervals, frame: gaps,
    over50ms: intervals.filter(ms => ms > 50).length,
    longTaskCount: raw.longTaskSupported ? raw.longTasks.length : null,
    longTaskTotalMs: raw.longTaskSupported ? raw.longTasks.reduce((total, entry) => total + entry.duration, 0) : null,
    nodes: raw.mutations.reduce((sum, record) => ({
      added: sum.added + record.added, removed: sum.removed + record.removed,
    }), { added: 0, removed: 0 }),
    nextFrame: summary(delays), nextFrameAfterDispatch: summary(dispatchDelays),
    inputCount: raw.inputEvents.length, moveCount: raw.moves.length,
    wheelCount: raw.wheelEvents.length,
  };
}

async function gesture(page, cdp, kind) {
  const target = { canvas: '#canvas', slider, scroll: '.adjustments' }[kind];
  const right = page.locator('.adjustments');
  await right.evaluate(el => { el.scrollTop = 0; });
  await page.waitForTimeout(300);
  let sliderFraction;
  if (kind === 'slider') {
    sliderFraction = await page.locator(slider).evaluate(el => {
      const proportion = (el.valueAsNumber - Number(el.min)) / (Number(el.max) - Number(el.min));
      const width = el.getBoundingClientRect().width;
      const thumbWidth = 14;
      return (thumbWidth / 2 + proportion * (width - thumbWidth)) / width;
    });
  }
  const boxStart = await xy(page, target, sliderFraction ?? .26, kind === 'slider' ? .5 : .42);
  const boxEnd = await xy(page, target, kind === 'slider' ? .85 : .74, kind === 'slider' ? .5 : .57);
  await page.mouse.move(boxStart.x, boxStart.y);
  const before = await page.evaluate(({ target, kind }) => {
    const el = document.querySelector(target);
    return kind === 'scroll' ? { top: el.scrollTop, max: el.scrollHeight - el.clientHeight } :
      { pose: document.querySelector('#value-azimuth').textContent, value: el.value };
  }, { target, kind });
  if (kind === 'scroll' && before.max <= 0) throw Error('右栏没有可滚动空间。');
  const startProbe = () => page.evaluate(args => window.wbProbe.start(args.duration, args.target), {
    duration: gestureDuration, target,
  });
  const mix = (begin, end, fraction) => begin + fraction * (end - begin);
  const moves = f => ({ type: 'mouseMoved', button: 'left', buttons: 1,
    x: mix(boxStart.x, boxEnd.x, f), y: mix(boxStart.y, boxEnd.y, f) });
  let accumulated = 0;
  const wheel = f => {
    const position = Math.round(f * (before.max + 24));
    const deltaY = position - accumulated;
    accumulated = position;
    return { type: 'mouseWheel', x: boxStart.x, y: boxStart.y, deltaX: 0, deltaY };
  };
  let schedule, startAt;
  if (kind === 'scroll') {
    startAt = await startProbe();
    schedule = await dispatchTimed(cdp, wheel);
  } else {
    await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', ...boxStart,
      button: 'left', buttons: 1, clickCount: 1 });
    try {
      startAt = await startProbe();
      schedule = await dispatchTimed(cdp, moves);
    }
    finally {
      await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...boxEnd,
        button: 'left', buttons: 0, clickCount: 1 });
    }
  }
  const raw = await page.evaluate(() => window.wbProbe.finish());
  const position = await page.evaluate(({ target, kind }) => {
    const el = document.querySelector(target);
    return kind === 'scroll' ? { top: el.scrollTop, max: el.scrollHeight - el.clientHeight } :
      { pose: document.querySelector('#value-azimuth').textContent, value: el.value };
  }, { target, kind });
  const metrics = summarizeProbe(raw);
  if (kind !== 'scroll' && before.pose === position.pose) throw Error('拖动后画布姿态没有变化 ' + kind);
  if (kind === 'scroll' && position.top < position.max - 1) throw Error('右栏没有滚到底。');
  if (schedule.some(event => event.error) || !metrics.frame.count) throw Error('动作采样未送达或没有记录到帧。');
  await page.waitForFunction(() => document.querySelector('#save-state').dataset.state === 'saved');
  return { startAt, before, position, boxStart, boxEnd, plannedRateHz: inputRate, schedule, raw, ...metrics };
}

async function idle(page, cdp) {
  const get = async () => Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics
    .map(metric => [metric.name, metric.value]));
  await page.evaluate(() => window.wbProbe.start(10000, null));
  const from = await get();
  await sleep(10000);
  const to = await get();
  const raw = await page.evaluate(() => window.wbProbe.finish());
  const { longTaskCount, longTaskTotalMs } = summarizeProbe(raw);
  const elapsedS = to.Timestamp - from.Timestamp;
  const taskDurationS = to.TaskDuration - from.TaskDuration;
  if (!Number.isFinite(taskDurationS) || elapsedS <= 0) throw Error('CDP 没有返回有效的 TaskDuration。');
  return { from, to, elapsedS, taskDurationS, cpuPercent: taskDurationS / elapsedS * 100,
    longTaskCount, longTaskTotalMs, raw };
}


async function run(){
  const samples=[];
  fs.mkdirSync(outDir,{recursive:true});
  const destination=path.join(outDir,'metrics.json');
  await withService(async service=>{
    await service.toggleRunner(true);
    for(const largest of [false,true]){
      for(const rate of [1,4]){
        const sample={model:largest?'最大预览 FBX 副本':'演示产品',rate};
        await withPage(async (page,cdp,context)=>{
          const errors=[];
          page.on('pageerror',error=>errors.push(error.message));
          page.on('console',message=>{if(message.type()==='error')errors.push(message.text());});
          await context.addInitScript(installProbe,{noEffects:false});
          await cdp.send('Network.enable');
          await cdp.send('Network.setCacheDisabled',{cacheDisabled:true});
          await cdp.send('Performance.enable',{timeDomain:'timeTicks'});
          await cdp.send('Emulation.setCPUThrottlingRate',{rate});
          const demo=prepare('bench-'+largest+'-'+rate,largest);
          await page.goto(url);await page.waitForFunction(()=>!!window.__whitebg);
          await sleep(1100);
          sample.homeIdle=await idle(page,cdp);
          await scan(page,demo);
          await page.locator('input[name=model]').evaluateAll(inputs=>{
            inputs.find(input=>input.value.toLowerCase().endsWith('.fbx')).click();
          });
          await page.locator('#create-task').click();
          await modelReady(page);
          await page.locator('#tab-lighting').click();
          await page.locator('#lighting-enabled').check();await page.locator('[data-light-preset="0"]').click();
          await page.waitForFunction(()=>window.__whitebg.task.lighting.azimuth_deg===-45
            &&document.getElementById('save-state').dataset.state==='saved');
          await page.locator('#tab-pose').click();
          const taskId=await page.evaluate(()=>window.__whitebg.task.id);
          sample.webgl=await page.locator('#canvas').evaluate(canvas=>{
            const gl=canvas.getContext('webgl2'),ext=gl.getExtension('WEBGL_debug_renderer_info');
            return gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
          });
          sample.meshes=await page.evaluate(()=>window.__whitebg.model.meshCount);
          sample.polls=0;
          page.on('request',request=>{if(request.method()==='GET'&&request.url()===url+'api/tasks/'+taskId)sample.polls++;});
          await sleep(1400);
          sample.taskIdle=await idle(page,cdp);
          await page.locator('#smallshot').click();
          await page.waitForFunction(()=>window.__whitebg.task.items.some(i=>i.smallshot.state==='queued'));
          service.advance();
          await done(page,'smallshot');
          await page.locator('#items img').first().waitFor();
          sample.canvas=await gesture(page,cdp,'canvas');
          await sleep(1000);
          sample.slider=await gesture(page,cdp,'slider');
          sample.errors=errors;
        });
        samples.push(sample);
        fs.writeFileSync(destination,JSON.stringify(samples,null,2));
        console.log(JSON.stringify({model:sample.model,rate,home:sample.homeIdle.cpuPercent,task:sample.taskIdle.cpuPercent,canvas:sample.canvas.fps,slider:sample.slider.fps}));
      }
    }
  });
}
run().catch(error=>{console.error(error);process.exitCode=1;});
