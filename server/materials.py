"""可编辑材质参数模型、参考材质绑定与有效覆盖合并逻辑（Contract 2）。"""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path

from .ref_library import effective_material_ref, material_key

MATERIAL_LIMITS = {
    'hue_shift_deg': (-15, 15),
    'saturation_mul': (0.6, 1.4),
    'brightness_mul': (0.5, 2.5),
    'contrast_mul': (0.7, 1.3),
    'reflection_mul': (0.0, 2.0),
    'glossiness_delta': (-0.3, 0.3),
}
GLASS_LIMITS = {
    'refraction_mul': (0.0, 1.5),
    'ior': (1.3, 1.8),
    'opacity_mul': (0.5, 1.0),
}
ENVIRONMENT_LIMITS = {
    'reflection_strength_mul': (0.0, 2.0),
}
NEUTRAL_NUMBERS = {
    'hue_shift_deg': 0.0,
    'saturation_mul': 1.0,
    'brightness_mul': 1.0,
    'contrast_mul': 1.0,
    'reflection_mul': 1.0,
    'glossiness_delta': 0.0,
    'refraction_mul': 1.0,
    'opacity_mul': 1.0,
    'reflection_strength_mul': 1.0,
    'intensity_mul': 1.0,
    'rotate_deg': 0.0,
}
ACTIVE_CALIBRATION_STATES = ('rendering', 'thinking')
_BAD_CONTROL_RE = re.compile(r'[\x00-\x1f\ud800-\udfff]')
_GLASS_HINT_TOKENS = ('玻璃', 'glass', 'glazing', '长虹', '清玻', '茶玻', '灰玻', '黑玻', '白玻')


def _clamp_value(value, low, high):
    from .core import StudioError

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StudioError('材质调整值必须是有限数值。', 400)
    return max(low, min(high, value))


def is_neutral_field(key, value):
    if key == 'ior':
        return value is None
    target = NEUTRAL_NUMBERS.get(key)
    if target is None:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    return abs(float(value) - target) < 1e-9


def has_non_neutral_overrides(overrides):
    """检查 overrides 是否包含任何非中性的材质、环境、灯光或主图调整。"""
    if not isinstance(overrides, dict):
        return False
    for settings in (overrides.get('materials') or {}).values():
        if isinstance(settings, dict):
            for key, value in settings.items():
                if (key in MATERIAL_LIMITS or key in GLASS_LIMITS) and not is_neutral_field(key, value):
                    return True
    env = overrides.get('environment')
    if isinstance(env, dict):
        for key, value in env.items():
            if key in ENVIRONMENT_LIMITS and not is_neutral_field(key, value):
                return True
    lights = overrides.get('lights')
    if isinstance(lights, dict):
        for key in ('intensity_mul', 'rotate_deg'):
            if key in lights and not is_neutral_field(key, lights[key]):
                return True
        for entry in (lights.get('per_light') or {}).values():
            if isinstance(entry, dict) and 'intensity_mul' in entry and not is_neutral_field('intensity_mul', entry['intensity_mul']):
                return True
    items = overrides.get('items')
    if isinstance(items, dict):
        for entry in items.values():
            if isinstance(entry, dict):
                rot = (entry.get('lights') or {}).get('rotate_deg')
                if rot is not None and not is_neutral_field('rotate_deg', rot):
                    return True
    return False


def effective_overrides(task) -> dict | None:
    """以 ai_calibration.accepted_overrides 为底，叠加用户手动编辑的 material_overrides。"""
    if not isinstance(task, dict):
        return None
    calibration = task.get('ai_calibration')
    accepted = calibration.get('accepted_overrides') if isinstance(calibration, dict) else None
    manual = task.get('material_overrides')
    accepted = copy.deepcopy(accepted) if isinstance(accepted, dict) else None
    manual = copy.deepcopy(manual) if isinstance(manual, dict) else None
    if accepted is None and manual is None:
        return None
    if accepted is not None and manual is None:
        return accepted if has_non_neutral_overrides(accepted) else None
    if accepted is None:
        merged = {
            'materials': copy.deepcopy(manual.get('materials') or {}) if isinstance(manual.get('materials'), dict) else {},
            'environment': copy.deepcopy(manual.get('environment') or {}) if isinstance(manual.get('environment'), dict) else {},
        }
        return merged if has_non_neutral_overrides(merged) else None

    merged = accepted
    merged_materials = merged.setdefault('materials', {})
    if not isinstance(merged_materials, dict):
        merged_materials = {}
        merged['materials'] = merged_materials
    for name, settings in (manual.get('materials') or {}).items():
        if isinstance(settings, dict):
            target = merged_materials.setdefault(name, {})
            if not isinstance(target, dict):
                target = {}
                merged_materials[name] = target
            target.update(copy.deepcopy(settings))
    merged_env = merged.setdefault('environment', {})
    if not isinstance(merged_env, dict):
        merged_env = {}
        merged['environment'] = merged_env
    if isinstance(manual.get('environment'), dict):
        for key, value in manual['environment'].items():
            merged_env[key] = copy.deepcopy(value)
    return merged if has_non_neutral_overrides(merged) else None


def load_scene_manifest(studio, task, jobs=None):
    """优先读取最新有效校准轮次的 scene_manifest.json，若无则读取 preview/scene_manifest.json。"""
    from .core import inside, read_json

    if jobs is None:
        jobs = studio.job_index.for_task(task['id'])
    task_folder = studio.task_dir(task['id'])
    by_id = {job['job_id']: job for job, _ in jobs if isinstance(job, dict) and 'job_id' in job}
    rounds = (task.get('ai_calibration') or {}).get('rounds') or []
    for record in reversed(rounds):
        if not isinstance(record, dict) or 'n' not in record:
            continue
        folder = by_id.get(record.get('job_id'), {}).get('calibration_dir')
        folder = Path(folder) if folder else task_folder / 'calibration' / f"round-{record['n']}"
        path = folder / 'scene_manifest.json'
        if not inside(path, task_folder) or path.is_symlink():
            continue
        try:
            manifest = read_json(path)
            if isinstance(manifest, dict) and isinstance(manifest.get('materials'), list):
                return manifest
        except (OSError, ValueError, KeyError, TypeError):
            continue

    preview_manifest = task_folder / 'preview' / 'scene_manifest.json'
    if inside(preview_manifest, task_folder) and preview_manifest.is_file() and not preview_manifest.is_symlink():
        try:
            manifest = read_json(preview_manifest)
            if isinstance(manifest, dict) and isinstance(manifest.get('materials'), list):
                return manifest
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return None


def scene_materials_detail(studio, task, jobs=None) -> dict:
    manifest = load_scene_manifest(studio, task, jobs)
    library = studio.refs.read()
    raw_bindings = task.get('material_bindings')
    bindings = dict(raw_bindings) if isinstance(raw_bindings, dict) else {}

    materials_out = []
    seen = set()
    for entry in (manifest or {}).get('materials') or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get('name')
        if not isinstance(name, str) or not name or name in seen:
            continue
        seen.add(name)
        auto_ref = material_key(name, library)
        bound_ref = bindings.get(name)
        if bound_ref is not None and not isinstance(bound_ref, str):
            bound_ref = None
        eff_ref = effective_material_ref(name, library, bindings)
        color = entry.get('color')
        bitmap = entry.get('bitmap')
        params = entry.get('params')
        objects = entry.get('objects')
        materials_out.append({
            'name': name,
            'kind': str(entry.get('kind') or 'other'),
            'class': str(entry.get('class') or ''),
            'objects': int(objects) if isinstance(objects, (int, float)) and not isinstance(objects, bool) else 0,
            'color': list(color) if isinstance(color, list) else None,
            'bitmap': bitmap if isinstance(bitmap, str) and bitmap else None,
            'params': copy.deepcopy(params) if isinstance(params, dict) else {},
            'auto_ref': auto_ref,
            'bound_ref': bound_ref,
            'effective_ref': eff_ref,
        })

    env = (manifest or {}).get('environment')
    reflect_map = (env.get('reflect_map')
                   if isinstance(env, dict) and isinstance(env.get('reflect_map'), str) and env.get('reflect_map')
                   else None)
    return {
        'materials': materials_out,
        'environment': {'reflect_map': reflect_map},
        'material_overrides': copy.deepcopy(task.get('material_overrides')),
        'effective_overrides': effective_overrides(task),
        'material_bindings': bindings,
    }


def _valid_target_material_name(name, known_materials):
    if not isinstance(name, str):
        return False
    if name in known_materials:
        return True
    stripped = name.strip()
    return bool(stripped) and name == stripped and len(stripped) <= 60 and not _BAD_CONTROL_RE.search(stripped)


def _is_glass_material(name, known_materials):
    if name in known_materials:
        return known_materials[name].get('kind') == 'glass'
    folded = name.casefold()
    return (not known_materials) or any(token in folded for token in _GLASS_HINT_TOKENS)


def _sanitize_material_overrides(raw_overrides, known_materials, accepted_overrides, *, fill_against_accepted=False):
    from .core import StudioError

    if raw_overrides is None:
        return None
    if not isinstance(raw_overrides, dict):
        raise StudioError('材质参数格式无效。', 400)
    raw_materials = raw_overrides.get('materials', {})
    raw_env = raw_overrides.get('environment', {})
    if raw_materials is not None and not isinstance(raw_materials, dict):
        raise StudioError('材质参数列表格式无效。', 400)
    if raw_env is not None and not isinstance(raw_env, dict):
        raise StudioError('环境反射参数格式无效。', 400)

    accepted_materials = (accepted_overrides or {}).get('materials') if isinstance(accepted_overrides, dict) else None
    accepted_materials = accepted_materials if isinstance(accepted_materials, dict) else {}
    accepted_env = (accepted_overrides or {}).get('environment') if isinstance(accepted_overrides, dict) else None
    accepted_env = accepted_env if isinstance(accepted_env, dict) else {}

    cleaned_materials = {}
    all_names = list((raw_materials or {}).keys())
    if fill_against_accepted:
        for acc_name in accepted_materials:
            if acc_name not in (raw_materials or {}):
                all_names.append(acc_name)

    for name in all_names:
        settings = (raw_materials or {}).get(name, {})
        if not _valid_target_material_name(name, known_materials):
            raise StudioError(f'材质名称无效：{name}', 400)
        if not isinstance(settings, dict):
            raise StudioError(f'材质「{name}」的参数必须是对象。', 400)
        limits = dict(MATERIAL_LIMITS)
        if _is_glass_material(name, known_materials):
            limits.update(GLASS_LIMITS)
        acc_mat = accepted_materials.get(name) if isinstance(accepted_materials.get(name), dict) else {}
        clamped = {}
        for key, value in settings.items():
            if key not in limits:
                continue
            clamped_val = None if (key == 'ior' and value is None) else _clamp_value(value, *limits[key])
            acc_non_neutral = key in acc_mat and not is_neutral_field(key, acc_mat[key])
            if not is_neutral_field(key, clamped_val) or acc_non_neutral:
                clamped[key] = clamped_val
        if fill_against_accepted:
            for key, acc_val in acc_mat.items():
                if key not in clamped and key not in settings and not is_neutral_field(key, acc_val):
                    clamped[key] = None if key == 'ior' else NEUTRAL_NUMBERS[key]
        if clamped:
            cleaned_materials[name] = clamped

    cleaned_env = {}
    if raw_env and 'reflection_strength_mul' in raw_env and raw_env['reflection_strength_mul'] is not None:
        val = _clamp_value(raw_env['reflection_strength_mul'], *ENVIRONMENT_LIMITS['reflection_strength_mul'])
        acc_env_non_neutral = ('reflection_strength_mul' in accepted_env
                               and not is_neutral_field('reflection_strength_mul', accepted_env['reflection_strength_mul']))
        if not is_neutral_field('reflection_strength_mul', val) or acc_env_non_neutral:
            cleaned_env['reflection_strength_mul'] = val
    elif fill_against_accepted and 'reflection_strength_mul' in accepted_env:
        if not is_neutral_field('reflection_strength_mul', accepted_env['reflection_strength_mul']):
            cleaned_env['reflection_strength_mul'] = NEUTRAL_NUMBERS['reflection_strength_mul']

    if not cleaned_materials and not cleaned_env:
        return None
    return {'materials': cleaned_materials, 'environment': cleaned_env}


def _sanitize_material_bindings(raw_bindings, known_materials, library_materials):
    from .core import StudioError

    if raw_bindings is None:
        return {}
    if not isinstance(raw_bindings, dict):
        raise StudioError('参考材质绑定格式无效。', 400)
    cleaned = {}
    for mat_name, target_ref in raw_bindings.items():
        if not _valid_target_material_name(mat_name, known_materials):
            raise StudioError(f'绑定的场景材质名称无效：{mat_name}', 400)
        if target_ref in (None, '', 'auto'):
            continue
        if target_ref == '__none__':
            cleaned[mat_name] = '__none__'
        elif isinstance(target_ref, str) and target_ref in library_materials:
            cleaned[mat_name] = target_ref
        else:
            raise StudioError(f'参考库中没有材质分组「{target_ref}」。', 400)
    return cleaned


def update_task_materials(studio, task_id, data) -> dict:
    from .core import StudioError

    if not isinstance(data, dict):
        raise StudioError('请求内容必须是 JSON 对象。', 400)

    with studio.lock:
        task = studio.merge(task_id)['task']
        jobs = studio.job_index.for_task(task_id)
        if (task.get('ai_calibration') or {}).get('state') in ACTIVE_CALIBRATION_STATES:
            raise StudioError('AI 校准进行中，请等本轮结束后再修改材质参数。', 409)

        action = data.get('action', 'set')
        manifest = load_scene_manifest(studio, task, jobs)
        known_materials = {
            m['name']: m
            for m in (manifest or {}).get('materials') or []
            if isinstance(m, dict) and isinstance(m.get('name'), str) and m['name']
        }
        accepted = (task.get('ai_calibration') or {}).get('accepted_overrides')

        if action == 'set':
            if 'material_overrides' not in data and 'material_bindings' not in data:
                raise StudioError('请提供要保存的材质参数或参考材质绑定。', 400)
            if 'material_overrides' in data:
                task['material_overrides'] = _sanitize_material_overrides(
                    data.get('material_overrides'), known_materials, accepted
                )
            if 'material_bindings' in data:
                library = studio.refs.read()
                cleaned_bindings = _sanitize_material_bindings(
                    data.get('material_bindings'), known_materials, library.get('materials') or {}
                )
                if cleaned_bindings:
                    task['material_bindings'] = cleaned_bindings
                else:
                    task.pop('material_bindings', None)

        elif action == 'from_round':
            raw_round = data.get('round')
            if isinstance(raw_round, bool) or raw_round is None:
                raise StudioError('请指定有效的校准轮次编号。', 400)
            try:
                round_no = int(raw_round)
            except (TypeError, ValueError):
                raise StudioError('请指定有效的校准轮次编号。', 400) from None
            rounds = (task.get('ai_calibration') or {}).get('rounds') or []
            record = next((r for r in rounds if isinstance(r, dict) and r.get('n') == round_no), None)
            if record is None:
                raise StudioError('没有找到这一轮校准记录。', 404)
            round_overrides = record.get('overrides') or {}
            task['material_overrides'] = _sanitize_material_overrides(
                {
                    'materials': copy.deepcopy(round_overrides.get('materials') or {}),
                    'environment': copy.deepcopy(round_overrides.get('environment') or {}),
                },
                known_materials,
                accepted,
                fill_against_accepted=True,
            )

        elif action == 'reset_material':
            name = data.get('name')
            if not isinstance(name, str) or not name.strip():
                raise StudioError('请指定要重置的材质名称。', 400)
            current = task.get('material_overrides')
            if isinstance(current, dict):
                mats = dict(current.get('materials') or {})
                env = dict(current.get('environment') or {})
                if name == '__environment__':
                    env = {}
                else:
                    mats.pop(name, None)
                task['material_overrides'] = {'materials': mats, 'environment': env} if (mats or env) else None
            else:
                task['material_overrides'] = None

        elif action == 'reset_all':
            task['material_overrides'] = None

        else:
            raise StudioError('材质操作无效。', 400)

        studio.save(task)
        return {'task': task, 'materials_detail': scene_materials_detail(studio, task, jobs)}
