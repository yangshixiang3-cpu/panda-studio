"""PF1 打光数据和中文描述；与 web/lighting.js 使用同一组验收样例。"""
import math

DEFAULT_LIGHTING = dict(enabled=False, azimuth_deg=None, elevation_deg=35, intensity=1.0, contrast=0.5)
MAX_LIGHTING_BYTES = 20 * 1024 * 1024


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def render_lighting_config(config):
    """保留作业提交时的原始调参，缺项由执行器按既有配置补齐。"""
    return {'lighting_tuning': config.get('lighting_tuning', {}),
            'key_light_intensity': config.get('key_light_intensity', 1)}


def validate_lighting(value):
    value = value if isinstance(value, dict) else {}
    result = dict(DEFAULT_LIGHTING)
    result['enabled'] = value.get('enabled') is True
    az = value.get('azimuth_deg')
    result['azimuth_deg'] = max(-180, min(180, az)) if finite(az) else None
    for key, low, high in [('elevation_deg', 5, 85), ('intensity', .6, 1.5), ('contrast', 0, 1)]:
        if finite(value.get(key)):
            result[key] = max(low, min(high, value[key]))
    return result


def merged_lighting(task, item):
    return validate_lighting(item.get('lighting') if item.get('lighting') is not None else task.get('lighting'))


def lighting_active(light):
    return light['enabled'] and light['azimuth_deg'] is not None


def effective_lighting(light):
    return light if lighting_active(light) else dict(DEFAULT_LIGHTING)


def lighting_text(value):
    light = validate_lighting(value)
    if not lighting_active(light):
        return '模板默认灯光'
    az, el, contrast = light['azimuth_deg'], light['elevation_deg'], light['contrast']
    side = '左' if az < 0 else '右'
    horizontal = ('正前方' if abs(az) < 20 else side+'前方' if abs(az) < 70
                  else side+'侧' if abs(az) <= 110 else side+'后方（逆光）')
    height = '低角度' if el < 20 else '斜上方' if el <= 55 else '高处（接近顶光）'
    faces = '正面受光、两侧略暗' if abs(az) < 20 else ('左侧受光较亮、右侧偏暗' if az < 0 else '右侧受光较亮、左侧偏暗')
    if el > 55:
        faces += '、顶面最亮'
    strength = '柔和' if contrast < .3 else '中等' if contrast <= .7 else '强烈'
    def number(n):
        # 统一用普通小数，避免两个语言的指数记法和负零不同。
        scaled = math.floor(abs(n)*1_000_000+.5)
        whole, fraction = divmod(scaled, 1_000_000)
        return ('−' if n < 0 and scaled else '') + str(whole) + ('.'+str(fraction).zfill(6).rstrip('0') if fraction else '')
    approx = math.floor(el/5+.5)*5  # 按 5° 取整，免得"约 40°"和括号里的"高 35°"看着矛盾
    return f'主光从{horizontal}{height}约 {approx}° 照来（水平 {number(az)}°，高 {number(el)}°）；{faces}；明暗对比{strength}。'


def protect_light_overrides(value, task):
    """一项启用打光即收紧全局调整，避免全局旋转破坏该项光向。"""
    import copy
    if value is None:
        return None
    result = copy.deepcopy(value)
    if any(i.get('enabled', True) and lighting_active(merged_lighting(task, i)) for i in task.get('items', [])):
        lights = result.setdefault('lights', {})
        lights.update(rotate_deg=0, intensity_mul=max(.85, min(1.15, lights.get('intensity_mul', 1))), per_light={})
        for settings in result.get('items', {}).values():
            settings.setdefault('lights', {})['rotate_deg'] = 0
    return result
