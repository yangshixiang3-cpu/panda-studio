import assert from 'node:assert/strict';
import fs from 'node:fs';
import module from 'node:module';
import path from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';

import {
  COMMON_PARAM_SPECS,
  GLASS_PARAM_SPECS,
  ENV_PARAM_SPEC,
  inferMaterialKind,
  materialKindLabel,
  materialSwatchCss,
  normalizeMaterialsDetail,
  collectEditableMaterials,
  countMaterialAdjustments,
  summarizeMaterialBindings,
  createMaterialsDraft,
  serializeMaterialsPayload,
  installMaterialsUI,
} from '../../web/materials.js';
import {describeOverrides} from '../../web/calibration.js';
import {shouldPauseBackground, syncBackgroundPause} from '../../web/task-state.js';
import {canvasPollDelay, MAX_CANVAS_POLL_FAILURES} from '../../web/canvas-model.js';

// 为 Node 单测映射浏览器 importmap 中的 three 路径
const threeEntry = new URL('../../web/vendor/three/build/three.module.js', import.meta.url).href;
const threeAddonsDir = new URL('../../web/vendor/three/examples/jsm/', import.meta.url).href;
module.registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'three') return {url: threeEntry, shortCircuit: true};
    if (specifier.startsWith('three/addons/')) {
      return {url: new URL(specifier.slice('three/addons/'.length), threeAddonsDir).href, shortCircuit: true};
    }
    return nextResolve(specifier, context);
  },
});

const THREE = await import('three');
const {FBXLoader} = await import('three/addons/loaders/FBXLoader.js');
const {detectVariants, withFilteredFbxWarnings} = await import('../../web/variants.js');
const {parseFbxAsset, parseFbx} = await import('../../web/model/loaders.js');

test('材质类型识别、中文标签与色块换算：不暴露内部英文参数名', () => {
  assert.equal(inferMaterialKind('摩卡红橡', 'wood'), 'wood');
  assert.equal(inferMaterialKind('超白长虹玻璃'), 'glass');
  assert.equal(inferMaterialKind('拉手_拉丝不锈钢'), 'metal');
  assert.equal(inferMaterialKind('门板_奶白烤漆'), 'solid');
  assert.equal(inferMaterialKind('板材01'), 'other');
  assert.equal(materialKindLabel('glass', '柜门'), '玻璃');
  assert.equal(materialKindLabel('', '北美黑胡桃'), '木纹');
  assert.equal(materialSwatchCss({name: '未知编码', color: [140, 95, 68]}), 'rgb(140, 95, 68)');
  assert.match(materialSwatchCss({name: '胡桃木'}) || '', /^#[0-9a-f]{6}$/i);

  const allLabels = [...COMMON_PARAM_SPECS, ...GLASS_PARAM_SPECS, ENV_PARAM_SPEC].map(s => s.label).join(' ');
  assert.doesNotMatch(allLabels, /hue_shift|brightness_mul|reflection_mul|glossiness|refraction|opacity|ior/i);
});

test('参考材质对应摘要与调整计数：覆盖自动匹配、手动指定、不对照与环境关闭', () => {
  const bundle = {
    materials_detail: {
      materials: [
        {name: '摩卡红橡', kind: 'wood', objects: 12, color: [140, 95, 68], bitmap: 'wood.jpg', auto_ref: '摩卡红橡', bound_ref: null, effective_ref: '摩卡红橡'},
        {name: '板材01', kind: 'other', objects: 4, auto_ref: null, bound_ref: '北美胡桃', effective_ref: '北美胡桃'},
        {name: '背板02', kind: 'solid', objects: 2, auto_ref: '奶白', bound_ref: '__none__', effective_ref: null},
        {name: '未知饰面', kind: 'other', objects: 1, auto_ref: null, bound_ref: null, effective_ref: null},
      ],
      environment: {reflect_map: 'studio.hdr'},
      material_overrides: {
        materials: {'摩卡红橡': {brightness_mul: 1.12}},
        environment: {reflection_strength_mul: 0},
      },
      effective_overrides: {
        materials: {'摩卡红橡': {brightness_mul: 1.12}},
        environment: {reflection_strength_mul: 0},
      },
      material_bindings: {
        '板材01': '北美胡桃',
        '背板02': '__none__',
      },
    },
  };
  const detail = normalizeMaterialsDetail(bundle, {});
  const summary = summarizeMaterialBindings(detail);
  assert.deepEqual(
    summary.map(item => [item.name, item.mode, item.targetText]),
    [
      ['摩卡红橡', 'auto', '摩卡红橡'],
      ['板材01', 'manual', '北美胡桃'],
      ['背板02', 'none', '不对照参考库'],
      ['未知饰面', 'unmatched', '未匹配'],
    ],
  );
  // 摩卡红橡（改参数）+ 板材01（手动绑定）+ 背板02（设为不对照）+ 环境反光强度（0%）= 4 项
  assert.equal(countMaterialAdjustments(detail, {}), 4);

  const withCustom = collectEditableMaterials(detail, ['抽屉板_预填']);
  assert.equal(withCustom.at(-1).name, '抽屉板_预填');
  assert.equal(withCustom.at(-1).custom, true);
});

test('材质草稿与 PUT 载荷序列化：范围截断、中性值剔除、AI 基线差分及折射率留空', () => {
  const accepted = {
    materials: {
      '摩卡红橡': {brightness_mul: 1.15, hue_shift_deg: 3},
      '长虹玻璃': {refraction_mul: 1.1, ior: 1.52},
    },
    environment: {reflection_strength_mul: 0.8},
  };
  const task = {ai_calibration: {accepted_round: 2, accepted_overrides: accepted}};
  const bundle = {
    task,
    scene_materials: ['摩卡红橡', '长虹玻璃'],
  };
  const detail = normalizeMaterialsDetail(bundle, task);
  const draft = createMaterialsDraft(detail, ['板材01']);

  // 未改动时：AI 已采用值不应重复写入手动 material_overrides
  const untouched = serializeMaterialsPayload(draft, accepted);
  assert.deepEqual(untouched, {
    material_overrides: {materials: {}, environment: {}},
    material_bindings: {},
  });

  // 手动把摩卡红橡亮度调回 100%（覆盖 AI 的 1.15），板材01 设饱和度与绑定，玻璃折射率清空，环境反光关到 0%
  draft.materials['摩卡红橡'].brightness_mul = 1;
  draft.materials['板材01'].saturation_mul = 9; // 超上限，序列化时应截断到 1.4
  draft.materials['长虹玻璃'].ior = null;
  draft.materials['长虹玻璃'].opacity_mul = 0.2; // 超下限，截断到 0.5
  draft.environment.reflection_strength_mul = 0;
  draft.bindings['板材01'] = '摩卡红橡';
  draft.bindings['摩卡红橡'] = '';

  const payload = serializeMaterialsPayload(draft, accepted);
  assert.deepEqual(payload, {
    material_overrides: {
      materials: {
        '摩卡红橡': {brightness_mul: 1},
        '长虹玻璃': {ior: null, opacity_mul: 0.5},
        '板材01': {saturation_mul: 1.4},
      },
      environment: {reflection_strength_mul: 0},
    },
    material_bindings: {
      '板材01': '摩卡红橡',
    },
  });
});

test('材质参数弹层交互：空清单预填、参考绑定、以此轮微调与保存动作', async () => {
  const makeClassList = () => {
    const set = new Set();
    return {
      add: (...xs) => xs.forEach(x => set.add(x)),
      remove: (...xs) => xs.forEach(x => set.delete(x)),
      toggle: (x, force) => {
        const on = force === undefined ? !set.has(x) : Boolean(force);
        if (on) set.add(x); else set.delete(x);
        return on;
      },
      contains: x => set.has(x),
    };
  };
  const badge = {hidden: true, textContent: ''};
  const openBtn = {title: '', onclick: null, classList: makeClassList()};
  const content = {
    innerHTML: '',
    querySelectorAll: () => [],
    querySelector: () => null,
  };
  const dialog = {
    open: false,
    scrollTop: 0,
    dataset: {},
    showModal() { this.open = true; },
    querySelectorAll: () => [],
  };
  const elements = new Map([
    ['materials-open', openBtn],
    ['materials-badge', badge],
    ['dialog', dialog],
    ['dialog-content', content],
    ['mat-custom-form', {}],
    ['materials-reset-all', {}],
    ['materials-save', {}],
  ]);

  const calls = [];
  const toasts = [];
  let task = {id: 'task-1', ai_calibration: {}};
  let bundle = {
    task,
    materials_detail: {
      materials: [],
      environment: {reflect_map: 'studio.hdr'},
      material_overrides: null,
      effective_overrides: null,
      material_bindings: {},
    },
  };

  const ui = installMaterialsUI({
    $: id => elements.get(id),
    api: async (url, body, method = 'GET') => {
      calls.push({url, body, method});
      if (url === '/api/refs') return {materials: {'摩卡红橡': {}, '长虹玻璃': {}}};
      const nextDetail = {
        ...bundle.materials_detail,
        material_overrides: body.action === 'from_round'
          ? {materials: {'摩卡红橡': {brightness_mul: 1.08}}, environment: {reflection_strength_mul: 0}}
          : (body.material_overrides || null),
        effective_overrides: body.action === 'from_round'
          ? {materials: {'摩卡红橡': {brightness_mul: 1.08}}, environment: {reflection_strength_mul: 0}}
          : (body.material_overrides || null),
        material_bindings: body.material_bindings || {},
      };
      return {task, materials_detail: nextDetail};
    },
    escape: s => String(s ?? ''),
    modal: (_title, html) => { content.innerHTML = html; dialog.showModal(); },
    guard: fn => fn,
    toast: msg => toasts.push(msg),
    flush: async () => {},
    refresh: async () => {},
    getTask: () => task,
    getBundle: () => bundle,
    onUpdated: res => { bundle = {...bundle, materials_detail: res.materials_detail}; },
  });

  ui.render();
  assert.equal(badge.hidden, true);

  await ui.open();
  assert.equal(dialog.dataset.view, 'materials');
  assert.match(content.innerHTML, /尚未读到场景材质清单/);
  assert.match(content.innerHTML, /设为 0% 可关闭环境反射贴图对木漆高光的影响/);
  assert.doesNotMatch(content.innerHTML.replace(/data-param-key="[^"]+"/g, ''), /hue_shift_deg|brightness_mul|reflection_mul/);

  await ui.openFromRound(2);
  assert.deepEqual(calls.at(-2), {
    url: '/api/tasks/task-1/materials',
    body: {action: 'from_round', round: 2},
    method: 'PUT',
  });
  assert.equal(badge.hidden, false);
  assert.equal(badge.textContent, '2');
  assert.match(toasts.at(-1), /第 2 轮/);
});

test('校准记录中文描述：环境反光为 0 时显示关闭环境反光', () => {
  const lines = describeOverrides({
    materials: {},
    lights: {},
    environment: {reflection_strength_mul: 0},
    items: {},
  });
  assert.deepEqual(lines, [{target: '环境反光', change: '关闭环境反光'}]);
});

test('2.7 背景动画按需暂停：任务页、画布页及窗口隐藏时挂暂停类名，不重复改写 DOM', () => {
  assert.equal(shouldPauseBackground('home', false), false);
  assert.equal(shouldPauseBackground('refs', false), false);
  assert.equal(shouldPauseBackground('settings', false), false);
  assert.equal(shouldPauseBackground('task', false), true);
  assert.equal(shouldPauseBackground('canvas', false), true);
  assert.equal(shouldPauseBackground('home', true), true);

  let toggles = 0;
  const classes = new Set();
  const doc = {
    hidden: false,
    body: {
      classList: {
        contains: c => classes.has(c),
        toggle(c, on) {
          toggles += 1;
          if (on) classes.add(c); else classes.delete(c);
        },
      },
    },
  };
  syncBackgroundPause(doc, 'home');
  assert.equal(classes.has('bg-paused'), false);
  assert.equal(toggles, 0);

  syncBackgroundPause(doc, 'task');
  assert.equal(classes.has('bg-paused'), true);
  assert.equal(classes.has('page-hidden'), false);
  const afterTask = toggles;

  // 同一状态重复同步不产生多余 classList 变更
  syncBackgroundPause(doc, 'task');
  assert.equal(toggles, afterTask);

  doc.hidden = true;
  syncBackgroundPause(doc, 'home');
  assert.equal(classes.has('bg-paused'), true);
  assert.equal(classes.has('page-hidden'), true);

  doc.hidden = false;
  syncBackgroundPause(doc, 'home');
  assert.equal(classes.has('bg-paused'), false);
  assert.equal(classes.has('page-hidden'), false);
});

test('2.9 FBX 局部警告过滤与单次解析复用：立即恢复 console.warn，不重复解析 ArrayBuffer', () => {
  const originalWarn = console.warn;
  const seen = [];
  const spyWarn = (...args) => seen.push(args.join(' '));
  console.warn = spyWarn;
  try {
    const res = withFilteredFbxWarnings(() => {
      console.warn('THREE.FBXLoader: ShininessExponent map is not supported');
      console.warn('FBXLoader: unknown property');
      console.warn('业务告警：贴图尺寸偏大');
      return 'ok';
    });
    assert.equal(res, 'ok');
    assert.equal(console.warn, spyWarn);
    assert.deepEqual(seen, ['业务告警：贴图尺寸偏大']);

    assert.throws(() => withFilteredFbxWarnings(() => {
      console.warn('THREE.FBXLoader: noisy warning');
      throw new Error('boom');
    }), /boom/);
    assert.equal(console.warn, spyWarn);
    assert.equal(seen.length, 1);
  } finally {
    console.warn = originalWarn;
  }

  // 验证已解析 Three 场景可直接用于款式识别，0 次触发 FBXLoader.prototype.parse
  const origParse = FBXLoader.prototype.parse;
  let parseCount = 0;
  FBXLoader.prototype.parse = function (...args) {
    parseCount += 1;
    return origParse.apply(this, args);
  };
  try {
    const group = new THREE.Group();
    const matA = new THREE.MeshStandardMaterial({name: '摩卡红橡'});
    const matB = new THREE.MeshStandardMaterial({name: '奶白烤漆'});
    const m1 = new THREE.Mesh(new THREE.BoxGeometry(80, 90, 50), matA);
    m1.name = '左柜体';
    m1.position.set(-120, 45, 0);
    const m2 = new THREE.Mesh(new THREE.BoxGeometry(80, 90, 50), matB);
    m2.name = '右柜体';
    m2.position.set(120, 45, 0);
    group.add(m1, m2);

    const variantsFromScene = detectVariants(group);
    assert.equal(parseCount, 0);
    assert.equal(variantsFromScene.length, 2);
    assert.deepEqual(variantsFromScene.map(v => v.nodes), [['左柜体'], ['右柜体']]);

    // 用真实测试夹具 FBX 验证 prepareFbx -> detectVariants -> parseFbx 全链路只调用一次 FBXLoader.parse
    const fixturesDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'fixtures');
    const fbxName = fs.existsSync(fixturesDir)
      ? fs.readdirSync(fixturesDir).find(f => f.toLowerCase().endsWith('.fbx'))
      : null;
    if (fbxName) {
      parseCount = 0;
      const raw = fs.readFileSync(path.join(fixturesDir, fbxName));
      const arrayBuffer = raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength);
      const warnBefore = console.warn;
      const prepared = parseFbxAsset(arrayBuffer, {taskId: 't-fbx'});
      assert.equal(console.warn, warnBefore);
      assert.equal(parseCount, 1);

      const detected = detectVariants(prepared.asset);
      assert.ok(Array.isArray(detected) && detected.length >= 1);
      assert.equal(parseCount, 1);

      const model = parseFbx(prepared, {taskId: 't-fbx'});
      assert.equal(parseCount, 1);
      assert.ok(model.meshCount >= 1);
      model.dispose();
    }
  } finally {
    FBXLoader.prototype.parse = origParse;
  }
});

test('2.12 画布生图轮询节流与连续失败退避', () => {
  assert.equal(MAX_CANVAS_POLL_FAILURES, 5);
  assert.equal(canvasPollDelay(0, 0), 0);
  assert.equal(canvasPollDelay(1, 0), 1500);
  assert.equal(canvasPollDelay(2, 0), 1500);
  assert.equal(canvasPollDelay(4, 0), 2300);
  assert.equal(canvasPollDelay(10, 0), 3000);
  assert.equal(canvasPollDelay(2, 1), 2000);
  assert.equal(canvasPollDelay(2, 2), 4000);
  assert.equal(canvasPollDelay(2, 3), 8000);
  assert.equal(canvasPollDelay(2, 5), 15000);
});
