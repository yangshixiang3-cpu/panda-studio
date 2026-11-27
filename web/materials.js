// 可编辑材质参数与参考库材质对应绑定：纯数据换算函数可供 Node 单测直接导入，UI 挂载接入任务页与 AI 校准弹层。
import {finishColorOf} from './finishes.js';
import {syncRange} from './range.js';

export const KIND_LABELS = Object.freeze({
  wood: '木纹',
  solid: '纯色',
  glass: '玻璃',
  metal: '金属',
  other: '其他',
});

const clampNum = (value, min, max) => Math.max(min, Math.min(max, value));
const roundTo = (value, digits = 2) => {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
};

export const COMMON_PARAM_SPECS = Object.freeze([
  {
    key: 'hue_shift_deg', label: '色调微调', min: -15, max: 15, step: 1, unit: '°',
    defaultRaw: 0, defaultDisplay: 0,
    toDisplay: raw => raw == null ? 0 : clampNum(Math.round(Number(raw)), -15, 15),
    fromDisplay: disp => clampNum(Math.round(Number(disp)), -15, 15),
  },
  {
    key: 'saturation_mul', label: '颜色浓淡', min: 60, max: 140, step: 1, unit: '%',
    defaultRaw: 1, defaultDisplay: 100,
    toDisplay: raw => raw == null ? 100 : clampNum(Math.round(Number(raw) * 100), 60, 140),
    fromDisplay: disp => roundTo(clampNum(Math.round(Number(disp)), 60, 140) / 100, 4),
  },
  {
    key: 'brightness_mul', label: '明亮程度', min: 50, max: 250, step: 1, unit: '%',
    defaultRaw: 1, defaultDisplay: 100,
    toDisplay: raw => raw == null ? 100 : clampNum(Math.round(Number(raw) * 100), 50, 250),
    fromDisplay: disp => roundTo(clampNum(Math.round(Number(disp)), 50, 250) / 100, 4),
  },
  {
    key: 'contrast_mul', label: '明暗反差', min: 70, max: 130, step: 1, unit: '%',
    defaultRaw: 1, defaultDisplay: 100,
    toDisplay: raw => raw == null ? 100 : clampNum(Math.round(Number(raw) * 100), 70, 130),
    fromDisplay: disp => roundTo(clampNum(Math.round(Number(disp)), 70, 130) / 100, 4),
  },
  {
    key: 'reflection_mul', label: '反光强度', min: 0, max: 200, step: 1, unit: '%',
    defaultRaw: 1, defaultDisplay: 100,
    toDisplay: raw => raw == null ? 100 : clampNum(Math.round(Number(raw) * 100), 0, 200),
    fromDisplay: disp => roundTo(clampNum(Math.round(Number(disp)), 0, 200) / 100, 4),
  },
  {
    key: 'glossiness_delta', label: '表面光泽', min: -0.3, max: 0.3, step: 0.01, unit: '',
    defaultRaw: 0, defaultDisplay: 0,
    toDisplay: raw => raw == null ? 0 : roundTo(clampNum(Number(raw), -0.3, 0.3), 2),
    fromDisplay: disp => roundTo(clampNum(Number(disp), -0.3, 0.3), 2),
  },
]);

export const GLASS_PARAM_SPECS = Object.freeze([
  {
    key: 'refraction_mul', label: '玻璃折射', min: 0, max: 150, step: 1, unit: '%',
    defaultRaw: 1, defaultDisplay: 100,
    toDisplay: raw => raw == null ? 100 : clampNum(Math.round(Number(raw) * 100), 0, 150),
    fromDisplay: disp => roundTo(clampNum(Math.round(Number(disp)), 0, 150) / 100, 4),
  },
  {
    key: 'ior', label: '折射率', min: 1.3, max: 1.8, step: 0.01, unit: '',
    nullable: true, sliderFallback: 1.52, placeholder: '模型默认',
    defaultRaw: null, defaultDisplay: '',
    toDisplay: raw => raw == null || raw === '' ? '' : roundTo(clampNum(Number(raw), 1.3, 1.8), 2),
    fromDisplay: disp => {
      if (disp == null || String(disp).trim() === '') return null;
      const num = Number(disp);
      return Number.isFinite(num) ? roundTo(clampNum(num, 1.3, 1.8), 2) : null;
    },
  },
  {
    key: 'opacity_mul', label: '不透明程度', min: 50, max: 100, step: 1, unit: '%',
    defaultRaw: 1, defaultDisplay: 100,
    toDisplay: raw => raw == null ? 100 : clampNum(Math.round(Number(raw) * 100), 50, 100),
    fromDisplay: disp => roundTo(clampNum(Math.round(Number(disp)), 50, 100) / 100, 4),
  },
]);

export const ENV_PARAM_SPEC = Object.freeze({
  key: 'reflection_strength_mul', label: '环境反光强度', min: 0, max: 200, step: 1, unit: '%',
  defaultRaw: 1, defaultDisplay: 100,
  toDisplay: raw => raw == null ? 100 : clampNum(Math.round(Number(raw) * 100), 0, 200),
  fromDisplay: disp => roundTo(clampNum(Math.round(Number(disp)), 0, 200) / 100, 4),
});

export const ALL_MATERIAL_PARAM_SPECS = Object.freeze([...COMMON_PARAM_SPECS, ...GLASS_PARAM_SPECS]);
const SPEC_BY_KEY = new Map(ALL_MATERIAL_PARAM_SPECS.map(spec => [spec.key, spec]));

export function inferMaterialKind(name = '', rawKind = '') {
  if (rawKind && Object.prototype.hasOwnProperty.call(KIND_LABELS, rawKind)) return rawKind;
  if (rawKind === 'paint' || rawKind === 'lacquer') return 'solid';
  if (rawKind === 'mirror') return 'metal';
  const text = String(name || '');
  if (/玻|glass|长虹/i.test(text)) return 'glass';
  if (/镜|五金|金属|不锈钢|银|铝|拉手|金|铜|香槟|steel|metal|chrome/i.test(text)) return 'metal';
  if (/木|橡|胡桃|樱桃|柚|白蜡|枫|松|榆|oak|walnut/i.test(text)) return 'wood';
  if (/白|黑|灰|奶|烤漆|纯色/i.test(text)) return 'solid';
  return 'other';
}

export function materialKindLabel(kind, name = '') {
  return KIND_LABELS[inferMaterialKind(name, kind)] || '其他';
}

export function materialSwatchCss(entry) {
  const c = entry?.color;
  if (Array.isArray(c) && c.length >= 3 && c.slice(0, 3).every(v => Number.isFinite(Number(v)))) {
    const [r, g, b] = c.slice(0, 3).map(v => clampNum(Math.round(Number(v)), 0, 255));
    return `rgb(${r}, ${g}, ${b})`;
  }
  const approx = finishColorOf(entry?.name || '');
  return approx !== null ? '#' + approx.toString(16).padStart(6, '0') : null;
}

const isNeutralValue = (spec, value) => {
  if (spec.nullable) return value == null || value === '';
  if (value == null) return true;
  return Math.abs(Number(value) - spec.defaultRaw) < 1e-6;
};

export function isMaterialOverrideActive(values) {
  if (!values || typeof values !== 'object') return false;
  return ALL_MATERIAL_PARAM_SPECS.some(spec => !isNeutralValue(spec, values[spec.key]));
}

export function hasGlassOverride(values) {
  if (!values || typeof values !== 'object') return false;
  return GLASS_PARAM_SPECS.some(spec => !isNeutralValue(spec, values[spec.key]));
}

export function isEnvironmentOverrideActive(environment) {
  if (!environment || typeof environment !== 'object') return false;
  return !isNeutralValue(ENV_PARAM_SPEC, environment.reflection_strength_mul);
}

export function mergeEffectiveOverrides(acceptedOverrides, manualOverrides) {
  if (!acceptedOverrides && !manualOverrides) return null;
  const materials = {};
  const names = new Set([
    ...Object.keys(acceptedOverrides?.materials || {}),
    ...Object.keys(manualOverrides?.materials || {}),
  ]);
  for (const name of names) {
    const merged = {
      ...(acceptedOverrides?.materials?.[name] || {}),
      ...(manualOverrides?.materials?.[name] || {}),
    };
    const cleaned = {};
    for (const spec of ALL_MATERIAL_PARAM_SPECS) {
      if (Object.prototype.hasOwnProperty.call(merged, spec.key) && !isNeutralValue(spec, merged[spec.key])) {
        cleaned[spec.key] = spec.fromDisplay(spec.toDisplay(merged[spec.key]));
      }
    }
    if (Object.keys(cleaned).length) materials[name] = cleaned;
  }
  const envMerged = {
    ...(acceptedOverrides?.environment || {}),
    ...(manualOverrides?.environment || {}),
  };
  const environment = {};
  if (Object.prototype.hasOwnProperty.call(envMerged, 'reflection_strength_mul')
      && !isNeutralValue(ENV_PARAM_SPEC, envMerged.reflection_strength_mul)) {
    environment.reflection_strength_mul = ENV_PARAM_SPEC.fromDisplay(
      ENV_PARAM_SPEC.toDisplay(envMerged.reflection_strength_mul)
    );
  }
  if (!Object.keys(materials).length && !Object.keys(environment).length) return null;
  return {materials, environment};
}

export function normalizeMaterialsDetail(bundle, task = bundle?.task) {
  const raw = bundle?.materials_detail;
  const bindings = {...(task?.material_bindings || {}), ...(raw?.material_bindings || {})};
  const manualOverrides = raw?.material_overrides ?? task?.material_overrides ?? null;
  const acceptedOverrides = task?.ai_calibration?.accepted_overrides ?? null;
  const effectiveOverrides = raw && 'effective_overrides' in raw
    ? raw.effective_overrides
    : mergeEffectiveOverrides(acceptedOverrides, manualOverrides);

  let materials = Array.isArray(raw?.materials) ? raw.materials.map(m => ({...m})) : null;
  if (!materials) {
    const fallbackNames = Array.isArray(bundle?.scene_materials) ? bundle.scene_materials : [];
    materials = fallbackNames.filter(Boolean).map(name => {
      const bound = bindings[name] ?? null;
      return {
        name,
        kind: inferMaterialKind(name),
        class: '',
        objects: null,
        color: null,
        bitmap: null,
        params: {},
        auto_ref: null,
        bound_ref: bound,
        effective_ref: bound === '__none__' ? null : bound,
      };
    });
  }
  return {
    materials,
    hasManifestMaterials: materials.length > 0,
    environment: raw?.environment || {reflect_map: null},
    material_overrides: manualOverrides,
    effective_overrides: effectiveOverrides,
    material_bindings: bindings,
  };
}

export function collectEditableMaterials(detail, extraNames = []) {
  const normalized = detail?.materials ? detail : normalizeMaterialsDetail({materials_detail: detail});
  const byName = new Map();
  const bindings = normalized.material_bindings || {};
  for (const m of normalized.materials || []) {
    if (!m?.name) continue;
    const bound = Object.prototype.hasOwnProperty.call(bindings, m.name) ? bindings[m.name] : (m.bound_ref ?? null);
    const autoRef = m.auto_ref ?? null;
    const effectiveRef = bound === '__none__' ? null : (bound || m.effective_ref || autoRef || null);
    byName.set(m.name, {
      ...m,
      kind: inferMaterialKind(m.name, m.kind),
      bound_ref: bound,
      auto_ref: autoRef,
      effective_ref: effectiveRef,
      custom: Boolean(m.custom),
    });
  }
  const extras = [
    ...Object.keys(normalized.material_overrides?.materials || {}),
    ...Object.keys(normalized.effective_overrides?.materials || {}),
    ...Object.keys(bindings),
    ...extraNames,
  ];
  for (const rawName of extras) {
    const name = String(rawName || '').trim();
    if (!name || byName.has(name)) continue;
    const bound = bindings[name] ?? null;
    byName.set(name, {
      name,
      kind: inferMaterialKind(name),
      class: '',
      objects: null,
      color: null,
      bitmap: null,
      params: {},
      auto_ref: null,
      bound_ref: bound,
      effective_ref: bound === '__none__' ? null : bound,
      custom: true,
    });
  }
  return [...byName.values()];
}

export function countMaterialAdjustments(detail, task) {
  const normalized = detail?.materials ? detail : normalizeMaterialsDetail({materials_detail: detail}, task);
  const configured = new Set();
  for (const [name, values] of Object.entries(normalized.effective_overrides?.materials || {})) {
    if (isMaterialOverrideActive(values)) configured.add(name);
  }
  for (const [name, target] of Object.entries(normalized.material_bindings || {})) {
    if (typeof target === 'string' && target.trim()) configured.add(name);
  }
  const envCount = isEnvironmentOverrideActive(normalized.effective_overrides?.environment) ? 1 : 0;
  return configured.size + envCount;
}

export function summarizeMaterialBindings(detail) {
  const list = collectEditableMaterials(detail);
  return list.map(m => {
    const bound = m.bound_ref;
    if (bound === '__none__') {
      return {
        name: m.name,
        mode: 'none',
        targetText: '不对照参考库',
        tooltip: `${m.name}：已设为不对照参考库`,
      };
    }
    if (bound) {
      return {
        name: m.name,
        mode: 'manual',
        targetText: bound,
        badge: '手动',
        tooltip: `${m.name} → ${bound}（手动指定参考材质）`,
      };
    }
    if (m.effective_ref) {
      return {
        name: m.name,
        mode: 'auto',
        targetText: m.effective_ref,
        tooltip: `${m.name} → ${m.effective_ref}（自动匹配）`,
      };
    }
    return {
      name: m.name,
      mode: 'unmatched',
      targetText: '未匹配',
      tooltip: `${m.name}：未匹配到参考库材质，可在「材质参数」里手动指定`,
    };
  });
}

export function createMaterialsDraft(detail, extraNames = []) {
  const normalized = detail?.materials ? detail : normalizeMaterialsDetail({materials_detail: detail});
  const materialsList = collectEditableMaterials(normalized, extraNames);
  const effective = normalized.effective_overrides || {};
  const draftMaterials = {};
  const bindings = {};
  for (const m of materialsList) {
    const source = effective.materials?.[m.name] || {};
    const entry = {};
    for (const spec of ALL_MATERIAL_PARAM_SPECS) {
      entry[spec.key] = Object.prototype.hasOwnProperty.call(source, spec.key)
        ? spec.fromDisplay(spec.toDisplay(source[spec.key]))
        : spec.defaultRaw;
    }
    draftMaterials[m.name] = entry;
    bindings[m.name] = m.bound_ref || '';
  }
  const envSource = effective.environment || {};
  const environment = {
    reflection_strength_mul: Object.prototype.hasOwnProperty.call(envSource, 'reflection_strength_mul')
      ? ENV_PARAM_SPEC.fromDisplay(ENV_PARAM_SPEC.toDisplay(envSource.reflection_strength_mul))
      : ENV_PARAM_SPEC.defaultRaw,
  };
  return {
    items: materialsList,
    materials: draftMaterials,
    bindings,
    environment,
    reflectMap: normalized.environment?.reflect_map || null,
  };
}

export function serializeMaterialsPayload(draft, acceptedOverrides = null) {
  const materials = {};
  for (const [name, values] of Object.entries(draft?.materials || {})) {
    const cleanName = String(name || '').trim();
    if (!cleanName || !values) continue;
    const acceptedMat = acceptedOverrides?.materials?.[cleanName] || null;
    const out = {};
    for (const spec of ALL_MATERIAL_PARAM_SPECS) {
      const val = spec.fromDisplay(spec.toDisplay(values[spec.key]));
      const hasAccepted = acceptedMat && Object.prototype.hasOwnProperty.call(acceptedMat, spec.key)
        && !isNeutralValue(spec, acceptedMat[spec.key]);
      if (hasAccepted) {
        const acceptedVal = spec.fromDisplay(spec.toDisplay(acceptedMat[spec.key]));
        if (val !== acceptedVal) out[spec.key] = val;
      } else if (!isNeutralValue(spec, val)) {
        out[spec.key] = val;
      }
    }
    if (Object.keys(out).length) materials[cleanName] = out;
  }

  const environment = {};
  const envVal = ENV_PARAM_SPEC.fromDisplay(ENV_PARAM_SPEC.toDisplay(draft?.environment?.reflection_strength_mul));
  const acceptedEnv = acceptedOverrides?.environment?.reflection_strength_mul;
  const hasAcceptedEnv = acceptedEnv != null && !isNeutralValue(ENV_PARAM_SPEC, acceptedEnv);
  if (hasAcceptedEnv) {
    const baseEnv = ENV_PARAM_SPEC.fromDisplay(ENV_PARAM_SPEC.toDisplay(acceptedEnv));
    if (envVal !== baseEnv) environment.reflection_strength_mul = envVal;
  } else if (!isNeutralValue(ENV_PARAM_SPEC, envVal)) {
    environment.reflection_strength_mul = envVal;
  }

  const material_bindings = {};
  for (const [name, target] of Object.entries(draft?.bindings || {})) {
    const cleanName = String(name || '').trim();
    const cleanTarget = typeof target === 'string' ? target.trim() : '';
    if (cleanName && cleanTarget) material_bindings[cleanName] = cleanTarget;
  }

  return {
    material_overrides: {materials, environment},
    material_bindings,
  };
}

const baseName = p => String(p || '').replaceAll('\\', '/').split('/').pop();

export function installMaterialsUI({
  $, api, escape, modal, guard, toast, flush, refresh,
  getTask, getBundle, onUpdated, calibrationBusy, openCalibration,
}) {
  let customNames = [];
  let customTaskId = '';
  let refGroups = [];
  let draft = null;
  let saving = false;
  let lastBadgeKey = '';

  const isOpen = () => $('dialog')?.open && $('dialog')?.dataset.view === 'materials';

  function ensureTaskScope() {
    const id = getTask()?.id || '';
    if (id !== customTaskId) {
      customTaskId = id;
      customNames = [];
    }
  }

  function currentDetail() {
    ensureTaskScope();
    return normalizeMaterialsDetail(getBundle(), getTask());
  }

  async function loadRefGroups() {
    try {
      const res = await api('/api/refs');
      refGroups = Object.keys(res?.materials || {});
    } catch {
      // 参考库暂不可读时保留已有分组列表，不阻塞参数编辑
    }
  }

  function formatSigned(spec, displayVal) {
    if (spec.nullable && (displayVal === '' || displayVal == null)) return '';
    if (spec.key === 'glossiness_delta') return Number(displayVal).toFixed(2);
    return String(displayVal);
  }

  function sliderValueFor(spec, rawVal) {
    if (spec.nullable) return rawVal == null || rawVal === '' ? spec.sliderFallback : spec.toDisplay(rawVal);
    return spec.toDisplay(rawVal);
  }

  function renderParamControl(scopeAttr, spec, rawVal, disabledAttr) {
    const disp = spec.toDisplay(rawVal);
    const sliderVal = sliderValueFor(spec, rawVal);
    const unset = spec.nullable && (rawVal == null || rawVal === '');
    const textVal = formatSigned(spec, disp);
    return `<div class="mat-param${unset ? ' is-unset' : ''}" ${scopeAttr} data-param-key="${spec.key}">
      <div class="mat-param-head">
        <span class="mat-param-label">${escape(spec.label)}</span>
        <span class="mat-num-wrap">
          <input type="number" class="mat-num" min="${spec.min}" max="${spec.max}" step="${spec.step}"
            value="${escape(textVal)}" ${spec.placeholder ? `placeholder="${escape(spec.placeholder)}"` : ''}
            aria-label="${escape(spec.label)}" ${disabledAttr}>
          ${spec.unit ? `<em class="mat-unit">${escape(spec.unit)}</em>` : ''}
          ${spec.nullable ? `<button type="button" class="plain mat-clear-num" title="恢复模型默认折射率" ${unset || disabledAttr ? 'hidden' : ''}>默认</button>` : ''}
        </span>
      </div>
      <input type="range" class="mat-range" min="${spec.min}" max="${spec.max}" step="${spec.step}"
        value="${sliderVal}" aria-label="${escape(spec.label)}滑块" ${disabledAttr}>
    </div>`;
  }

  function bindingOptionsHtml(m) {
    const autoText = `自动匹配（${m.auto_ref || '未匹配'}）`;
    const current = draft.bindings[m.name] || '';
    const groups = [...refGroups];
    if (current && current !== '__none__' && !groups.includes(current)) groups.unshift(current);
    return `<option value="" ${current === '' ? 'selected' : ''}>${escape(autoText)}</option>`
      + `<option value="__none__" ${current === '__none__' ? 'selected' : ''}>不对照参考库</option>`
      + groups.map(g => `<option value="${escape(g)}" ${current === g ? 'selected' : ''}>${escape(g)}</option>`).join('');
  }

  function renderMaterialCard(m, index, disabledAttr) {
    const values = draft.materials[m.name] || {};
    const swatch = materialSwatchCss(m);
    const kindLabel = materialKindLabel(m.kind, m.name);
    const glassOpen = m.kind === 'glass' || hasGlassOverride(values);
    const active = isMaterialOverrideActive(values);
    const bound = draft.bindings[m.name] || '';
    const metaParts = [];
    if (m.bitmap) metaParts.push(`贴图：${baseName(m.bitmap)}`);
    if (Number.isFinite(m.objects) && m.objects > 0) metaParts.push(`${m.objects} 个部件`);
    if (m.custom) metaParts.push('手动预填');

    return `<section class="mat-card${active || bound ? ' is-configured' : ''}" data-mat-index="${index}">
      <div class="mat-card-head">
        <div class="mat-identity">
          <span class="mat-swatch" style="${swatch ? `background:${swatch}` : ''}" aria-hidden="true"></span>
          <strong class="mat-name">${escape(m.name)}</strong>
          <span class="mat-kind kind-${escape(m.kind)}">${escape(kindLabel)}</span>
          ${metaParts.length ? `<small class="mat-meta">${escape(metaParts.join(' · '))}</small>` : ''}
        </div>
        <button type="button" class="plain mat-reset-one" data-reset-mat="${index}" ${disabledAttr}>恢复默认</button>
      </div>
      <div class="mat-bind-row">
        <label for="mat-bind-${index}">对照参考材质</label>
        <select id="mat-bind-${index}" data-bind-mat="${index}" ${disabledAttr}>${bindingOptionsHtml(m)}</select>
        <small class="mat-bind-hint">${bound === '__none__' ? 'AI 校准时不引用参考库材质图' : bound ? `AI 校准时强制对照「${escape(bound)}」` : m.auto_ref ? `默认对应参考库「${escape(m.auto_ref)}」` : '参考库暂无同名材质，可手动指定'}</small>
      </div>
      <div class="mat-grid">
        ${COMMON_PARAM_SPECS.map(spec => renderParamControl(`data-mat="${index}"`, spec, values[spec.key], disabledAttr)).join('')}
      </div>
      <details class="mat-glass-details" ${glassOpen ? 'open' : ''}>
        <summary>玻璃参数${m.kind === 'glass' ? '' : '（玻璃材质展开可调）'}</summary>
        <div class="mat-grid mat-glass-grid">
          ${GLASS_PARAM_SPECS.map(spec => renderParamControl(`data-mat="${index}"`, spec, values[spec.key], disabledAttr)).join('')}
        </div>
      </details>
    </section>`;
  }

  function renderDialog(rebuildDraft = true) {
    const task = getTask();
    if (!task) return;
    const detail = currentDetail();
    if (rebuildDraft || !draft) draft = createMaterialsDraft(detail, customNames);
    const locked = Boolean(calibrationBusy?.()) || saving;
    const disabledAttr = locked ? 'disabled' : '';
    const acceptedRound = task.ai_calibration?.accepted_round;
    const reflectMap = draft.reflectMap ? `（当前贴图：${baseName(draft.reflectMap)}）` : '';

    const html = `<div class="mat-toolbar">
        <p class="hint mat-intro">微调颜色、明暗、光泽与环境反光，或为场景材质指定对照的参考库材质。${acceptedRound != null ? `当前叠加在 AI 第 ${acceptedRound} 轮调整之上。` : ''}</p>
        ${openCalibration ? '<button type="button" id="materials-to-calibration">AI 校准</button>' : ''}
      </div>
      ${calibrationBusy?.() ? '<p class="notice">AI 校准进行中，请等本轮结束后再修改材质参数。</p>' : ''}
      ${!detail.hasManifestMaterials ? '<div class="mat-empty-notice"><strong>尚未读到场景材质清单</strong><p class="hint">导出预览或出一次小样后会自动列出全部场景材质；你也可以先在下方输入 Max 里的材质名预先设置。</p></div>' : ''}
      <form id="mat-custom-form" class="mat-custom-bar" autocomplete="off">
        <input type="text" id="mat-custom-name" maxlength="80" placeholder="输入场景材质名称预填（例如：板材01、柜门_胡桃）" aria-label="手动添加场景材质名称" ${disabledAttr}>
        <button type="submit" id="mat-custom-add" ${disabledAttr}>添加材质</button>
      </form>
      <div class="mat-list">
        ${draft.items.length
          ? draft.items.map((m, idx) => renderMaterialCard(m, idx, disabledAttr)).join('')
          : '<div class="empty">暂无可编辑材质，可在上方输入材质名预填，或先导出预览／小样。</div>'}
      </div>
      <section class="mat-env-card" aria-label="全局环境">
        <div class="mat-card-head">
          <div class="mat-identity"><strong>全局环境</strong>${reflectMap ? `<small class="mat-meta">${escape(reflectMap)}</small>` : ''}</div>
        </div>
        <div class="mat-grid mat-env-grid">
          ${renderParamControl('data-env="1"', ENV_PARAM_SPEC, draft.environment.reflection_strength_mul, disabledAttr)}
        </div>
        <p class="hint mat-env-hint">设为 0% 可关闭环境反射贴图对木漆高光的影响</p>
      </section>
      <div class="dialog-actions mat-footer">
        <button type="button" id="materials-reset-all" ${disabledAttr}>全部恢复默认</button>
        <button type="button" id="materials-save" class="primary" ${disabledAttr}>${saving ? '保存中…' : '保存参数'}</button>
      </div>`;

    const dialog = $('dialog');
    const keepScroll = isOpen() ? dialog.scrollTop : 0;
    modal('材质参数', html);
    dialog.dataset.view = 'materials';
    dialog.querySelectorAll('input.mat-range').forEach(r => syncRange(r));
    if (keepScroll) dialog.scrollTop = keepScroll;
    bindDialogEvents();
  }

  function writeParamToDraft(container, spec, rawVal) {
    if (container.dataset.env) {
      draft.environment[spec.key] = rawVal;
      return;
    }
    const item = draft.items[Number(container.dataset.mat)];
    if (!item) return;
    draft.materials[item.name][spec.key] = rawVal;
    const card = container.closest('.mat-card');
    if (card) {
      const bound = draft.bindings[item.name] || '';
      card.classList.toggle('is-configured', isMaterialOverrideActive(draft.materials[item.name]) || Boolean(bound));
    }
  }

  function bindDialogEvents() {
    const content = $('dialog-content');
    if ($('materials-to-calibration')) {
      $('materials-to-calibration').onclick = () => openCalibration?.();
    }
    $('mat-custom-form').onsubmit = event => {
      event.preventDefault();
      const input = $('mat-custom-name');
      const name = input.value.replace(/[\x00-\x1f]/g, '').trim();
      if (!name) { input.focus(); return; }
      if (!customNames.includes(name)) customNames.push(name);
      if (!draft.items.some(i => i.name === name)) {
        const kind = inferMaterialKind(name);
        draft.items.push({
          name, kind, class: '', objects: null, color: null, bitmap: null,
          params: {}, auto_ref: null, bound_ref: null, effective_ref: null, custom: true,
        });
        draft.materials[name] = Object.fromEntries(ALL_MATERIAL_PARAM_SPECS.map(s => [s.key, s.defaultRaw]));
        draft.bindings[name] = '';
      }
      input.value = '';
      renderDialog(false);
    };

    content.querySelectorAll('[data-bind-mat]').forEach(select => {
      select.onchange = () => {
        const m = draft.items[Number(select.dataset.bindMat)];
        if (!m) return;
        const val = select.value;
        draft.bindings[m.name] = val;
        const hint = select.parentElement.querySelector('.mat-bind-hint');
        if (hint) {
          hint.textContent = val === '__none__'
            ? 'AI 校准时不引用参考库材质图'
            : val ? `AI 校准时强制对照「${val}」`
            : m.auto_ref ? `默认对应参考库「${m.auto_ref}」` : '参考库暂无同名材质，可手动指定';
        }
        select.closest('.mat-card')?.classList.toggle(
          'is-configured',
          isMaterialOverrideActive(draft.materials[m.name]) || Boolean(val)
        );
      };
    });

    content.querySelectorAll('.mat-param').forEach(box => {
      const key = box.dataset.paramKey;
      const spec = key === ENV_PARAM_SPEC.key ? ENV_PARAM_SPEC : SPEC_BY_KEY.get(key);
      if (!spec) return;
      const numInput = box.querySelector('.mat-num');
      const rangeInput = box.querySelector('.mat-range');
      const clearBtn = box.querySelector('.mat-clear-num');

      rangeInput.oninput = () => {
        syncRange(rangeInput);
        const raw = spec.fromDisplay(rangeInput.value);
        const disp = spec.toDisplay(raw);
        numInput.value = formatSigned(spec, disp);
        box.classList.remove('is-unset');
        if (clearBtn) clearBtn.hidden = false;
        writeParamToDraft(box, spec, raw);
      };

      numInput.oninput = () => {
        const text = numInput.value.trim();
        if (spec.nullable && text === '') {
          box.classList.add('is-unset');
          if (clearBtn) clearBtn.hidden = true;
          syncRange(rangeInput, spec.sliderFallback);
          writeParamToDraft(box, spec, null);
          return;
        }
        const parsed = Number(text);
        if (!Number.isFinite(parsed)) return;
        const clampedDisp = clampNum(parsed, spec.min, spec.max);
        syncRange(rangeInput, clampedDisp);
        box.classList.remove('is-unset');
        if (clearBtn) clearBtn.hidden = false;
        writeParamToDraft(box, spec, spec.fromDisplay(clampedDisp));
      };

      numInput.onchange = () => {
        const text = numInput.value.trim();
        if (spec.nullable && text === '') {
          numInput.value = '';
          box.classList.add('is-unset');
          if (clearBtn) clearBtn.hidden = true;
          syncRange(rangeInput, spec.sliderFallback);
          writeParamToDraft(box, spec, null);
          return;
        }
        const raw = spec.fromDisplay(text === '' ? spec.defaultDisplay : text);
        const disp = spec.toDisplay(raw);
        numInput.value = formatSigned(spec, disp);
        box.classList.toggle('is-unset', Boolean(spec.nullable && raw == null));
        if (clearBtn) clearBtn.hidden = raw == null;
        syncRange(rangeInput, sliderValueFor(spec, raw));
        writeParamToDraft(box, spec, raw);
      };

      if (clearBtn) {
        clearBtn.onclick = () => {
          numInput.value = '';
          box.classList.add('is-unset');
          clearBtn.hidden = true;
          syncRange(rangeInput, spec.sliderFallback);
          writeParamToDraft(box, spec, null);
        };
      }
    });

    content.querySelectorAll('[data-reset-mat]').forEach(btn => {
      btn.onclick = guard(async () => {
        const m = draft.items[Number(btn.dataset.resetMat)];
        if (!m) return;
        await runAction({action: 'reset_material', name: m.name}, `已恢复「${m.name}」默认参数`);
      });
    });

    $('materials-reset-all').onclick = guard(() => runAction({action: 'reset_all'}, '已恢复全部默认材质参数'));
    $('materials-save').onclick = guard(async () => {
      const payload = serializeMaterialsPayload(draft, getTask()?.ai_calibration?.accepted_overrides);
      await runAction({action: 'set', ...payload}, '已保存，下次出小样或正式渲染立即生效');
    });
  }

  async function runAction(body, successToast) {
    if (saving || !getTask()) return;
    const id = getTask().id;
    saving = true;
    if (isOpen()) renderDialog(false);
    try {
      await flush?.();
      if (getTask()?.id !== id) return;
      const result = await api('/api/tasks/' + encodeURIComponent(id) + '/materials', body, 'PUT');
      if (getTask()?.id !== id) return;
      onUpdated?.(result);
      draft = createMaterialsDraft(currentDetail(), customNames);
      render();
      if (isOpen()) renderDialog(false);
      await refresh?.();
      if (successToast) toast(successToast);
      return result;
    } finally {
      saving = false;
      if (isOpen() && getTask()?.id === id) renderDialog(false);
    }
  }

  async function open() {
    if (!getTask()) return;
    ensureTaskScope();
    draft = createMaterialsDraft(currentDetail(), customNames);
    renderDialog(false);
    await loadRefGroups();
    if (isOpen()) renderDialog(false);
  }

  async function openFromRound(roundNo) {
    if (!getTask()) return;
    ensureTaskScope();
    const id = getTask().id;
    await flush?.();
    if (getTask()?.id !== id) return;
    const result = await api('/api/tasks/' + encodeURIComponent(id) + '/materials', {
      action: 'from_round',
      round: Number(roundNo),
    }, 'PUT');
    if (getTask()?.id !== id) return;
    onUpdated?.(result);
    await loadRefGroups();
    if (getTask()?.id !== id) return;
    draft = createMaterialsDraft(currentDetail(), customNames);
    render();
    renderDialog(false);
    refresh?.().catch(() => {});
    toast(`已载入第 ${roundNo} 轮参数，可继续微调`);
  }

  function render() {
    const btn = $('materials-open');
    const badge = $('materials-badge');
    if (!btn || !badge) return;
    const task = getTask();
    if (!task) return;
    const count = countMaterialAdjustments(currentDetail(), task);
    const title = count > 0 ? `已调整 ${count} 项材质参数或参考对应` : '编辑材质参数与对照参考材质';
    const key = `${count}|${title}`;
    if (key !== lastBadgeKey) {
      lastBadgeKey = key;
      const show = count > 0;
      if (badge.hidden === show) badge.hidden = !show;
      const text = show ? String(count) : '';
      if (badge.textContent !== text) badge.textContent = text;
      if (btn.title !== title) btn.title = title;
      if (btn.classList.contains('has-overrides') !== show) btn.classList.toggle('has-overrides', show);
    }
  }

  const openBtn = $('materials-open');
  if (openBtn) openBtn.onclick = guard(open);

  return {open, openFromRound, render, isOpen};
}
