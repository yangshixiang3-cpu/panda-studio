"""§11.5 数组协议的假 Codex；只读标准输入，在隔离测试目录写结果。"""
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.calibration import validate_schema
from server.core import atomic_json, read_json


def array_overrides(value):
    value = copy.deepcopy(value or {})
    neutral = dict(hue_shift_deg=0, saturation_mul=1, brightness_mul=1, contrast_mul=1,
                   reflection_mul=1, glossiness_delta=0, refraction_mul=1, ior=None, opacity_mul=1)
    lights = value.get('lights', {})
    return {'materials': [dict(neutral, **settings, name=name) for name, settings in value.get('materials', {}).items()],
            'lights': {'intensity_mul': lights.get('intensity_mul', 1), 'rotate_deg': lights.get('rotate_deg', 0),
                       'per_light': [dict(settings, name=name) for name, settings in lights.get('per_light', {}).items()]},
            'environment': {'reflection_strength_mul': value.get('environment', {}).get('reflection_strength_mul', 1)},
            'items': [dict(no=int(no), rotate_deg=settings['lights']['rotate_deg'])
                      for no, settings in value.get('items', {}).items()]}


def main():
    args = sys.argv[1:]
    schema = read_json(args[args.index('--output-schema')+1])
    output = Path(args[args.index('-o')+1])
    payload = json.loads(sys.stdin.read().split('本轮数据：\n', 1)[1])
    overrides = copy.deepcopy(payload['applied_overrides'] or {'materials': {}})
    ok, notes = payload['round'] >= 2, []
    if not ok:
        stats = next(iter(payload['stats']['items'].values()))
        if stats:
            name = next(iter(stats))
            mean = stats[name]['mean_srgb'] or [128]*3
            overrides['materials'][name] = dict(hue_shift_deg=2, saturation_mul=1.02,
                brightness_mul=1.03 if sum(mean)/3 < 200 else .98, contrast_mul=1,
                reflection_mul=.98, glossiness_delta=-.01)
            notes = [dict(target=name, observation='木色略偏冷，表面反光稍强。', change='木色稍微调暖，减弱一点反光。')]
    result = dict(verdict='ok' if ok else 'adjust', summary='颜色与质感已接近参考，可以确认。' if ok else '建议把木色稍微调暖，减弱一点反光。',
                  overrides=array_overrides(overrides), notes=notes, confidence=.92 if ok else .83)
    validate_schema(result, schema)
    atomic_json(output, result)
    print('假 Codex 已根据颜色统计完成本轮分析。')


if __name__ == '__main__':
    main()
