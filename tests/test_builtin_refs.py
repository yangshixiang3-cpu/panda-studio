"""内置材质参考：refs/builtin.json 的条目在读库时补进用户参考库。"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.ref_library import RefLibrary  # noqa: E402


class BuiltinRefsTest(unittest.TestCase):
    def setUp(self):
        (Path(__file__).parent / 'tmp').mkdir(exist_ok=True)   # 刚克隆的仓库里没有 tests/tmp
        self.root = Path(tempfile.mkdtemp(prefix='builtin-refs-', dir=Path(__file__).parent / 'tmp'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        refs = self.root / 'refs'
        (refs / '胡桃木').mkdir(parents=True)
        for name in ('a.jpg', 'b.jpg'):
            (refs / '胡桃木' / name).write_bytes(b'\xff\xd8fake')
        (refs / 'builtin.json').write_text(json.dumps({
            'materials': {
                '胡桃木': {'keywords': ['胡桃', '黑胡桃'], 'images': [
                    {'file': 'refs/胡桃木/a.jpg', 'source': 'x', 'status': 'approved'},
                    {'file': 'refs/胡桃木/b.jpg', 'source': 'y', 'status': 'approved'},
                    {'file': 'refs/胡桃木/丢失.jpg', 'source': 'z', 'status': 'approved'}]},
                '新材质': {'keywords': [], 'images': []}},
            'style': [], 'annotated': []}, ensure_ascii=False), encoding='utf-8')

    def write_library(self, library):
        (self.root / 'refs' / 'library.json').write_text(json.dumps(library, ensure_ascii=False), encoding='utf-8')

    def test_missing_builtins_are_added_and_flagged(self):
        self.write_library({'materials': {'胡桃木': {'keywords': ['胡桃'], 'images': []}}, 'style': [], 'annotated': []})
        library = RefLibrary(self.root).read()
        files = [e['file'] for e in library['materials']['胡桃木']['images']]
        self.assertEqual(files, ['refs/胡桃木/a.jpg', 'refs/胡桃木/b.jpg'])          # 文件不存在的跳过
        self.assertTrue(all(e['builtin'] for e in library['materials']['胡桃木']['images']))
        self.assertIn('黑胡桃', library['materials']['胡桃木']['keywords'])           # 关键词合并
        self.assertIn('新材质', library['materials'])

    def test_user_status_and_own_images_are_kept(self):
        self.write_library({'materials': {'胡桃木': {'keywords': ['胡桃'], 'images': [
            {'file': 'refs/胡桃木/a.jpg', 'source': 'x', 'status': 'rejected', 'builtin': True},
            {'file': 'refs/胡桃木/mine.jpg', 'source': 'me', 'status': 'approved'}]}}, 'style': [], 'annotated': []})
        images = RefLibrary(self.root).read()['materials']['胡桃木']['images']
        self.assertEqual([e['status'] for e in images if e['file'] == 'refs/胡桃木/a.jpg'], ['rejected'])
        self.assertIn('refs/胡桃木/mine.jpg', [e['file'] for e in images])
        self.assertEqual(len([e for e in images if e['file'] == 'refs/胡桃木/a.jpg']), 1)

    def test_no_builtin_file_keeps_old_behaviour(self):
        (self.root / 'refs' / 'builtin.json').unlink()
        self.write_library({'materials': {}, 'style': [], 'annotated': []})
        self.assertEqual(RefLibrary(self.root).read()['materials'], {})


if __name__ == '__main__':
    unittest.main()
