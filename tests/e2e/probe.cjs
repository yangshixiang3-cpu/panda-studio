// 这些函数在页面内运行，使用同一条 performance 时间轴。
function installProbe({ noEffects }) {
  const installStyle = () => {
    if (!noEffects || !document.head) return false;
    const style = document.createElement('style');
    style.id = 'wb09b-no-effects';
    style.textContent = '*{backdrop-filter:none!important;animation:none!important;transition:none!important}';
    document.head.append(style);
    return true;
  };
  if (noEffects && !installStyle()) {
    const observer = new MutationObserver(() => { if (installStyle()) observer.disconnect(); });
    observer.observe(document, { childList: true, subtree: true });
  }

  const tests = new Map();
  let recording = null;
  const get = id => document.getElementById(id);
  const visible = id => {
    const element = get(id);
    return element && !element.closest('[hidden]') && element.getClientRects().length > 0;
  };
  const feedback = mode => {
    const state = get('items').querySelector('.state:is(.queued,.rendering)');
    if (state) return state.textContent;
    const toast = get('toast');
    if (!toast.hidden && toast.textContent.startsWith(mode === 'smallshot' ? '已提交小样' : '已提交渲染')) {
      return toast.textContent;
    }
    if (get('items').querySelector('.state.done')) {
      const first = window.__whitebg?.task?.items.find(item => item.enabled);
      if (first?.[mode].state === 'done') return get('items').querySelector('.state.done').textContent;
    }
    return false;
  };
  function ready(name, previous) {
    const app = window.__whitebg;
    switch (name) {
      case 'home': return app && visible('start-page') && !get('product-path').disabled;
      case 'filled': return get('product-path').value.length > 0;
      case 'scan': return visible('scan-results') && !!get('models').querySelector('input') && !get('create-task').disabled;
      case 'model-selected': return !!get('models').querySelector('input:checked');
      case 'task': return visible('task-page') && !!get('items').querySelector('.selected') && !!get('task-number').textContent;
      case 'ready': return app?.model && !visible('loading-model') && !!get('items').querySelector('.selected') && !!get('pose-description').textContent && visible('canvas');
      case 'ai-off': return !get('ai-enabled').checked && app?.task?.ai_calibration.enabled === false;
      case 'row': return get('items').querySelector('.selected')?.dataset.index === String(previous);
      case 'drag': return get('pose-description').textContent !== previous;
      case 'small-feedback': return feedback('smallshot');
      case 'final-feedback': return feedback('final');
      case 'small-done':
      case 'final-done': {
        const items = app?.task?.items.filter(item => item.enabled);
        const key = name === 'small-done' ? 'smallshot' : 'final';
        return !!items?.length && items.every(item => item[key].state === 'done');
      }
      case 'overview': {
        const imgs = [...document.querySelectorAll('.overview-card img')];
        return get('dialog').open && imgs.length === previous && imgs.every(img => img.complete && img.naturalWidth);
      }
      case 'dialog-closed': return !get('dialog').open;
      case 'delivery': return get('dialog').open && visible('confirm-delivery') && !get('confirm-delivery').disabled;
      case 'written': return get('dialog').open && get('dialog-title').textContent === '入库完成' && !!get('delivery-result').textContent;
    }
    throw Error('未知条件 ' + name);
  }

  function check(entry, method) {
    if (entry.eventAt == null || entry.result) return;
    if (entry.condition === 'switch') {
      if (method === 'animation' && entry.draws.length && ready('row', entry.previous)) {
        entry.result = { eventAt: entry.eventAt, firstDrawEnd: entry.draws[0],
          lastDrawEnd: entry.draws.at(-1), at: performance.now(), method, draws: entry.draws };
      }
      return;
    }
    const value = ready(entry.condition, entry.previous);
    if (value) entry.result = { eventAt: entry.eventAt, at: performance.now(),
      method, evidence: typeof value === 'string' ? value : undefined };
  }

  function arm({ id, selector, event, condition, previous }) {
    const entry = { condition, previous, eventAt: null, result: null, draws: [] };
    const observer = new MutationObserver(() => check(entry, 'mutation'));
    const start = performance.now();
    let frame;
    const onInput = ev => {
      if (!ev.isTrusted || !ev.target.closest?.(selector)) return;
      entry.eventAt = ev.timeStamp;
      check(entry, 'capture');
      document.removeEventListener(event, onInput, true);
    };
    const finish = () => {
      observer.disconnect();
      if (event) document.removeEventListener(event, onInput, true);
      cancelAnimationFrame(frame);
    };
    function tick() {
      check(entry, 'animation');
      if (entry.result || performance.now() - start > 45000) finish();
      else frame = requestAnimationFrame(tick);
    }
    if (event) document.addEventListener(event, onInput, true);
    else entry.eventAt = start;
    observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true,
      attributeFilter: ['hidden', 'disabled', 'open', 'class'] });
    frame = requestAnimationFrame(tick);
    tests.set(id, entry);
  }

  async function instrumentSwitch() {
    const { Viewer } = await import('/viewer.js');
    const render = Viewer.prototype.render;
    Viewer.prototype.render = function (...args) {
      const answer = render.apply(this, args);
      for (const entry of tests.values()) {
        if (entry.condition === 'switch' && entry.eventAt != null && !entry.result && ready('row', entry.previous)) {
          entry.draws.push(performance.now());
        }
      }
      return answer;
    };
  }

  function start(duration, target) {
    const startAt = performance.now();
    const endAt = startAt + duration;
    const data = { startAt, endAt, frames: [], longTasks: [], mutations: [],
      moves: [], inputEvents: [], wheelEvents: [], animationCallbacks: [] };
    const longTaskSupported = PerformanceObserver.supportedEntryTypes.includes('longtask');
    data.longTaskSupported = longTaskSupported;
    function addLongTasks(entries) {
      for (const entry of entries) {
        if (entry.startTime >= startAt && entry.startTime < endAt) {
          data.longTasks.push(entry.toJSON());
        }
      }
    }
    let longObserver;
    if (longTaskSupported) {
      longObserver = new PerformanceObserver(list => addLongTasks(list.getEntries()));
      longObserver.observe({ type: 'longtask' });
    }
    const items = get('items');
    const mutationObserver = new MutationObserver(records => {
      const time = performance.now();
      for (const record of records) {
        if (time >= endAt) break;
        data.mutations.push({ at: time, added: record.addedNodes.length, removed: record.removedNodes.length });
      }
    });
    if (items && target) mutationObserver.observe(items, { childList: true, subtree: true });

    let pending = [];
    const listen = ev => {
      const deliveredAt = performance.now();
      if (deliveredAt >= endAt || !ev.isTrusted || !ev.target.closest?.(target)) return;
      const event = { at: ev.timeStamp, deliveredAt, x: ev.clientX, y: ev.clientY };
      if (ev.type === 'pointermove' && ev.buttons === 1) {
        data.moves.push(event);
        pending.push(event);
      } else if (ev.type === 'input') {
        data.inputEvents.push({ ...event, value: ev.target.value });
      } else if (ev.type === 'wheel') data.wheelEvents.push({ ...event, deltaY: ev.deltaY });
    };
    const eventTypes = ['pointermove', 'input', 'wheel'];
    if (target) for (const type of eventTypes) document.addEventListener(type, listen, true);
    if (target) {
      const next = () => requestAnimationFrame(stamp => {
        const at = performance.now();
        data.frames.push(stamp);
        data.animationCallbacks.push(at);
        for (const event of pending) event.nextFrameAt = at;
        pending = [];
        if (at < endAt) next();
      });
      next();
    }
    const finished = new Promise(resolve => setTimeout(() => {
      if (longObserver) {
        addLongTasks(longObserver.takeRecords());
        longObserver.disconnect();
      }
      mutationObserver.disconnect();
      if (target) for (const type of eventTypes) document.removeEventListener(type, listen, true);
      data.observerStoppedAt = performance.now();
      resolve(data);
    }, duration + 125));
    recording = { data, finished };
    return startAt;
  }
  window.wbProbe = { ready, arm, instrumentSwitch, start,
    result: id => tests.get(id)?.result, finish: () => recording.finished };
}

module.exports = { installProbe };
