"""开发替身；读取同一 schema 和统计，不访问网络、不消费额度。"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.calibration import validate_schema
from server.core import atomic_json, read_json


def main():
    args = sys.argv[1:]
    schema = read_json(args[args.index('--output-schema')+1])
    output = Path(args[args.index('-o')+1])
    prompt = sys.stdin.read()
    payload = json.loads(prompt.split('本轮数据：\n', 1)[1])
    overrides = payload['applied_overrides'] or {'materials': {}, 'lights': {'per_light': {}}, 'environment': {}, 'items': {}}
    ok = payload['round'] >= 2
    notes = []
    if not ok:
        names = list(next(iter(payload['stats']['items'].values())))
        if names:
            name = names[0]
            mean = next(iter(payload['stats']['items'].values()))[name]['mean_srgb'] or [128]*3
            overrides['materials'][name] = {'hue_shift_deg': 2, 'saturation_mul': 1.02,
                                            'brightness_mul': 1.03 if sum(mean)/3 < 200 else .98,
                                            'contrast_mul': 1, 'reflection_mul': .98, 'glossiness_delta': -.01}
            notes = [{'target': name, 'observation': '木色略偏冷，表面反光稍强。', 'change': '木色稍微调暖，减弱一点反光。'}]
    result = {'verdict': 'ok' if ok else 'adjust', 'summary': '颜色与质感已接近参考，可以确认。' if ok else '建议把木色稍微调暖，减弱一点反光。',
              'overrides': overrides, 'notes': notes, 'confidence': .92 if ok else .83}
    validate_schema(result, schema)
    atomic_json(output, result)
    print('假 Codex 已根据颜色统计完成本轮分析。')


if __name__ == '__main__':
    main()
