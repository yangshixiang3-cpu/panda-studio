"""创建独立的开发临时产品，不触碰真实产品目录。"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from server.core import atomic_json


def prepare():
    temp=ROOT/'tests'/'tmp';temp.mkdir(parents=True,exist_ok=True)
    product=Path(tempfile.mkdtemp(prefix='e2e-',dir=temp))/'测试咖啡柜'
    for name in ('模型','参考图','sku'):(product/name).mkdir(parents=True,exist_ok=True)
    (product/'模型'/'开发占位.max').write_text('仅供 fake_runner 流程测试，不是真实 Max 模型，不能交给 Max 执行器。',encoding='utf-8')
    shutil.copy2(ROOT/'tests'/'fixtures'/'L型咖啡柜_绑定.fbx',product/'模型'/'L型咖啡柜_绑定.fbx')
    shutil.copy2(ROOT/'tests'/'fixtures'/'侧面.png',product/'参考图'/'侧面.png')
    atomic_json(ROOT/'tests'/'demo-product.json',{'product_dir':str(product)})
    return product


if __name__=='__main__':print(json.dumps({'product_dir':str(prepare())},ensure_ascii=False))
