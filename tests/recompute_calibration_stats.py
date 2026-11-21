"""只读实测输入，重算 §11.7 分区统计，报告写到 tests/。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.color_stats import image_statistics
from server.core import atomic_json, read_json


def recompute(source):
    legend = read_json(source/'regions/legend.json')
    inputs = [source/'regions/legend.json']
    for no in (1, 2):
        inputs.extend((source/'regions'/f'{no}.png', source/'smallshots'/f'{no}.jpg'))
    hashes = lambda: {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    before = hashes()
    result = {'source': str(source), 'distance': '原色与 gamma 编码色最近欧氏距离 ≤ 30，黑色背景不计',
              'area_denominator': '整张小样像素数（包括背景）', 'items': {}}
    total = 0
    for no in (1, 2):
        sample = source/'smallshots'/f'{no}.jpg'
        stats = image_statistics(sample, source/'regions'/f'{no}.png', legend)
        with Image.open(sample) as image:
            size = list(image.size)
        count = size[0]*size[1]
        total += count
        if not all(s['pixels'] > 0 for s in stats.values()):
            raise AssertionError('实测图例中的材质必须都有非零像素。')
        result['items'][str(no)] = {'size': size, 'total_pixels': count, 'materials': stats}
    result['aggregate'] = {name: {'pixels': sum(item['materials'][name]['pixels'] for item in result['items'].values())}
                           for name in legend.values()}
    for stats in result['aggregate'].values():
        stats['area_ratio'] = stats['pixels']/total
    result['total_pixels'] = total
    result['inputs_unchanged'] = hashes() == before
    result['input_sha256'] = before
    if not result['inputs_unchanged']:
        raise AssertionError('实测输入文件发生变化。')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT/'exchange/tasks/20261120-103600-056c/calibration/round-0')
    args = parser.parse_args()
    report = recompute(args.source.resolve())
    output = ROOT/'tests/calibration-real-stats.json'
    atomic_json(output, report)
    print(json.dumps({'report': str(output), 'items': {no: item['materials'] for no, item in report['items'].items()},
                      'aggregate': report['aggregate'], 'inputs_unchanged': report['inputs_unchanged']}, ensure_ascii=False, indent=2))
