"""使用隔离产品和临时端口验证贴图接口。"""
import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.app import LocalServer, handler_for
from server.core import Studio, read_json
from test_temp_dirs import test_directory


class TextureEndpointTests(unittest.TestCase):
    def setUp(self):
        root = test_directory(self, 'textures-')
        self.studio = Studio(root, read_json(ROOT / 'config.json'))
        self.product = root / '测试产品'
        self.model = self.product / '模型' / '柜体'
        self.model.mkdir(parents=True)
        (self.model / '柜体.max').touch()
        self.task = self.studio.create({'product_dir': str(self.product)})
        self.server = LocalServer(('127.0.0.1', 0), handler_for(self.studio))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get(self, name, task_id=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request('GET', '/api/tasks/' + (task_id or self.task['id']) +
                               '/texture?' + urlencode({'name': name}))
            response = connection.getresponse()
            return response.status, response.getheader('Content-Type'), response.read()
        finally:
            connection.close()

    def test_model_directory_has_priority(self):
        for folder, content in ((self.model, b'model'), (self.model / 'maps', b'maps'),
                                (self.product, b'product')):
            self.write(folder / '焦糖胡桃.jpg', content)
        self.assertEqual(self.get('焦糖胡桃.jpg'), (200, 'image/jpeg', b'model'))

    def test_maps_and_texture_directories_have_priority_and_ignore_case(self):
        self.write(self.product / 'WOOD.jpg', b'product')
        self.write(self.model / '贴图' / 'wood.JPG', b'texture')
        self.write(self.model / 'Maps' / 'wOoD.JpG', b'maps')
        self.write(self.model / '贴图' / '石材.PNG', b'other')
        self.assertEqual(self.get('WOOD.jpg'), (200, 'image/jpeg', b'maps'))
        self.assertEqual(self.get('石材.png'), (200, 'image/png', b'other'))

    def test_breadth_first_search_depth_and_excluded_directories(self):
        self.write(self.product / '1' / '2' / '3' / '4' / '5' / '深层.bmp', b'five')
        self.write(self.product / '1' / '2' / '3' / '4' / '5' / '6' / '深层外.png', b'six')
        self.write(self.product / 'A' / '子级' / '浅层.webp', b'deeper')
        self.write(self.product / 'Z' / '浅层.webp', b'nearest')
        self.write(self.product / 'SKU' / '跳过.jpg', b'sku')
        self.write(self.product / '资料' / '旧版' / '跳过.jpg', b'old')
        self.assertEqual(self.get('深层.BMP'), (200, 'image/bmp', b'five'))
        self.assertEqual(self.get('浅层.webp'), (200, 'image/webp', b'nearest'))
        for name in ('深层外.png', '跳过.jpg'):
            with self.subTest(name=name):
                self.assertEqual(self.get(name)[0], 404)

    def test_filename_validation_and_missing_json(self):
        for name in ('../木.jpg', 'maps/木.jpg', r'maps\木.jpg', '木..jpg',
                     'C:木.jpg', '木.jpg:private.jpg', '木\x00.jpg', '木max.max', ''):
            with self.subTest(name=name):
                status, content_type, body = self.get(name)
                self.assertEqual(status, 400)
                self.assertIn('application/json', content_type)
                self.assertIn('error', json.loads(body))
        status, content_type, body = self.get('不存在.jpeg')
        self.assertEqual(status, 404)
        self.assertIn('application/json', content_type)
        self.assertIn('不存在.jpeg', json.loads(body)['error'])

    def test_results_are_cached_by_task_and_casefolded_name(self):
        self.write(self.product / 'Wood.jpeg', b'product')
        self.assertEqual(self.get('wood.jpeg')[2], b'product')
        self.write(self.model / 'Wood.jpeg', b'model')
        self.assertEqual(self.get('WOOD.JPEG')[2], b'product')
        other = self.studio.create({'product_dir': str(self.product)})
        self.assertEqual(self.get('wood.jpeg', other['id'])[2], b'model')
