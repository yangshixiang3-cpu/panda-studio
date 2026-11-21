"""只读历史白底，生成本地参考库缩图。"""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.core import ROOT, atomic_json, read_json
from server.ref_library import build_library


def main():
    parser = argparse.ArgumentParser(description='建立白底参考库，原图只读')
    parser.add_argument('--history-root', action='append', help='可重复指定历史根目录')
    args = parser.parse_args()
    config = read_json(ROOT/'config.json')
    roots = args.history_root or config.get('history_roots', [r'D:\产品资料'])
    report = build_library(ROOT, roots)
    atomic_json(ROOT/'refs'/'build-report.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
