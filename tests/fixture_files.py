"""测试用的产品模型和产品图。

这两个是公司产品素材，公开库（panda-studio）里不带；用到它们的测试在文件缺失时自动跳过，其余测试照常运行。
"""
from pathlib import Path
import unittest

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
PRODUCT_FBX = FIXTURES / 'L型咖啡柜_绑定.fbx'
PRODUCT_IMAGE = FIXTURES / '侧面.png'

needs_product_fbx = unittest.skipUnless(PRODUCT_FBX.is_file(), '公开版不含公司产品模型 tests/fixtures/L型咖啡柜_绑定.fbx')
