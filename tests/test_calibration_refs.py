"""本任务材质参考的隔离测试，AI 校准只调用假 Codex。"""
import io
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
from test_temp_dirs import test_directory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.app import handler_for
from server.calibration import match_task_region, select_images, snapshot_task_refs, task_ref_crop
from server.cleanup import ExchangeCleanup
from server.color_stats import srgb_to_lab
from server.core import Studio, StudioError, atomic_json, now, read_json
from server.ref_library import annotation_crop
from tools.fake_runner import run_once
from fixture_files import needs_product_fbx


def region(material, source, box=None):
    return {'material': material, 'material_source': source, 'box': box or [.05, .1, .2, .5]}


def usage(entry, index=None):
    return {'id': entry['id'], 'region_index': index}


class CalibrationReferenceTests(unittest.TestCase):
    def setUp(self):
        self.root = test_directory(self, 'calibration-refs-')
        config = read_json(ROOT/'config.json')
        config.update(exchange_dir='exchange', calibration_auto=False, calibration_max_rounds=1,
                      codex_command=[sys.executable, '-B', str(ROOT/'tests'/'fake_codex.py')],
                      smallshot_size={'portrait': [60, 80], 'landscape': [80, 60]})
        self.studio = Studio(self.root, config)
        self.addCleanup(self.studio.calibration.close)
        self.product = self.root/'产品'
        self.product.mkdir()
        (self.product/'柜.max').touch()
        self.id = self.studio.create({'product_dir': str(self.product)})['id']
        self.folder = self.studio.task_dir(self.id)
        self.calibration = self.studio.calibration
        self.library = self.studio.refs
        image = Image.new('RGB', (2500, 40), (110, 45, 35))
        image.paste((30, 90, 170), (1250, 0, 2500, 40))
        stream = io.BytesIO()
        image.save(stream, 'PNG')
        self.content = stream.getvalue()

    def upload(self, name='色卡.png'):
        return self.calibration.upload_ref(self.id, name, self.content)

    def update(self, entry, regions, in_library=False):
        return self.calibration.change_ref(self.id, entry['id'], {'regions': regions, 'in_library': in_library})['entry']

    def add_manifest(self, n, *materials):
        task = self.studio.load(self.id)
        task['ai_calibration']['rounds'].append(self.calibration.record(n, f'旧轮次-{n}', None))
        self.studio.save(task)
        atomic_json(self.folder/'calibration'/f'round-{n}'/'scene_manifest.json',
                    {'materials': [{'name': name} for name in materials]})

    def test_scene_library_new_matching_and_exact_names(self):
        names = ['柜门 胡桃饰面', '柜门胡桃木', 'Iron Frame']
        library = self.library.read()
        self.assertEqual(match_task_region(region('柜门 胡桃饰面', 'scene'), names, library), names[:1])
        self.assertEqual(match_task_region(region('胡桃饰面', 'scene'), names, library), names[:2])
        self.assertEqual(match_task_region(region('胡桃木', 'library'), names, library), names[:2])
        self.assertEqual(match_task_region(region('柜门', 'new'), names, library), names[:2])
        self.assertEqual(match_task_region(region('iron frame 试色', 'new'), names, library), names[2:])
        self.assertEqual(match_task_region(region('自定义红色', 'new'), names, library), [])

    def test_selection_priority_limit_and_lab(self):
        name = '柜门 胡桃'
        self.add_manifest(0, name)
        entry = self.update(self.upload(), [region(name, 'scene'), region('胡桃木', 'library'),
                                            region('胡桃', 'new'), region('样板橙', 'new')])
        plain = self.upload('整体.png')
        old = self.library.save_annotation([region('胡桃木', 'library')], source='库裁图.png', content=self.content)
        library = self.library.read()
        approved = self.library.add('胡桃木', '已认可.png', self.content, library)
        approved['status'] = 'approved'
        self.library.save(library)

        def select(n):
            task = self.studio.load(self.id)
            snapshot_task_refs(self.folder, n, task['ai_calibration']['task_refs'])
            return select_images(self.studio, task, {'n': n}, {'materials': [{'name': name}]}, {'items': []})

        images, roles = select(1)
        self.assertEqual([d.get('task_ref') for d in roles],
                         [usage(entry, 0), usage(entry, 1), usage(entry, 3), usage(plain), None, None])
        with Image.open(images[0]) as image:
            lab = ', '.join(f'{value:.1f}' for value in srgb_to_lab(image.getpixel((0, 0))))
        self.assertEqual(roles[0]['role'],
                         f'用户为本任务提供的参考，优先级最高，对应场景材质「{name}」（参考材质：{name}，本任务标注参考，目标 Lab≈({lab})）')
        self.assertIn('其他参考（样板橙）', roles[2]['role'])
        self.assertIn('整体参考', roles[3]['role'])
        self.assertEqual(images[-2].name, Path(annotation_crop(old['file'], 0)).name)
        self.assertEqual(images[-1], self.root/approved['file'])
        for index in range(12):
            self.upload(f'整体-{index}.png')
        images, roles = select(2)
        self.assertEqual(len(images), 12)
        self.assertTrue(all('task_ref' in description for description in roles))
        self.assertEqual([description['task_ref'] for description in roles[:4]],
                         [usage(entry, 0), usage(entry, 1), usage(entry, 3), usage(plain)])

    @needs_product_fbx
    def test_running_round_uses_snapshot_and_next_round_sees_changes(self):
        left = region('摩卡红橡', 'library')
        right = region('摩卡红橡', 'library', [.7, .1, .2, .5])
        entry = self.update(self.upload(), [left])
        removed = self.upload('将删除.png')
        started = self.calibration.action(self.id, {'action': 'start'})
        self.assertEqual(started['ai_calibration']['state'], 'rendering')
        self.update(entry, [right])
        self.calibration.change_ref(self.id, removed['id'])
        added = self.upload('新参考.png')
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            run_once(self.studio.exchange, next((ROOT/'tests'/'fixtures').glob('*.fbx')))
            calibration = self.studio.load(self.id)['ai_calibration']
            if not self.calibration.active:
                break
            time.sleep(.05)
        self.assertEqual(calibration['state'], 'waiting_user', calibration['rounds'])
        first, second = calibration['rounds']
        self.assertEqual(first['task_refs_used'], [usage(entry, 0), usage(removed)])
        self.assertEqual(second['task_refs_used'], [usage(entry, 0), usage(added)])
        self.assertEqual([ref['id'] for ref in calibration['task_refs']], [entry['id'], added['id']])
        labs = []
        for n in (0, 1):
            used = next(d for d in read_json(self.folder/'calibration'/f'round-{n}'/'input.json')['images']
                        if d.get('task_ref') == usage(entry, 0))
            labs.append(used['role'].split('Lab≈', 1)[1])
        self.assertNotEqual(*labs)

    def test_library_copy_only_keeps_library_regions_and_delete_is_local(self):
        self.assertNotIn('scene_materials', self.studio.describe(self.id))
        self.add_manifest(0, ' 材质大小写 A ')
        self.add_manifest(1, ' 材质大小写 A ', '正面')
        self.assertEqual(self.studio.describe(self.id)['scene_materials'], [' 材质大小写 A ', '正面'])
        entry = self.upload()
        original = self.folder/entry['file']
        with Image.open(original) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(max(image.size), 2400)
        library_box = region('摩卡红橡', 'library')
        regions = [region(' 材质大小写 A ', 'scene'), region('自定义', 'new'), library_box]
        with self.assertRaises(StudioError):
            self.update(entry, [region('正面材质', 'scene')])
        self.update(entry, regions, True)
        library_entry, = self.library.read()['annotated']
        # 每个框都收进参考库：场景材质对不上关键词、新材质，都新建同名分组（去掉首尾空格）
        self.assertEqual(library_entry['regions'], [{'material': '材质大小写 A', 'box': regions[0]['box']},
                                                    {'material': '自定义', 'box': regions[1]['box']},
                                                    {k: library_box[k] for k in ('material', 'box')}])
        self.assertIn('自定义', self.library.read()['materials'])
        self.assertIn('材质大小写 A', self.library.read()['materials'])
        self.assertEqual(self.studio.load(self.id)['ai_calibration']['task_refs'][0]['library_file'], library_entry['file'])
        self.update(entry, regions, True)
        self.assertEqual(self.library.read()['annotated'], [library_entry])
        self.update(entry, regions, False)                       # 旧的「不加入参考库」选项不再起作用，也不会多出一份
        self.assertEqual(self.library.read()['annotated'], [library_entry])
        crop = self.folder/task_ref_crop(entry['file'], 0)
        self.assertTrue(crop.is_file())
        self.calibration.change_ref(self.id, entry['id'])
        self.assertFalse(original.exists())
        self.assertFalse(crop.exists())
        self.assertEqual(self.studio.describe(self.id)['task']['ai_calibration']['task_refs'], [])
        self.assertEqual(self.library.read()['annotated'], [library_entry])
        self.assertTrue((self.root/annotation_crop(library_entry['file'], 0)).is_file())

    def test_every_task_ref_goes_to_library_and_same_image_merges(self):
        first = self.update(self.upload(), [region('摩卡红橡', 'library'), region('柜门 胡桃饰面', 'new', [.6, .1, .2, .5])])
        library = self.library.read()
        entry, = library['annotated']
        self.assertEqual([r['material'] for r in entry['regions']], ['摩卡红橡', '胡桃木'])   # 新材质名按关键词归到胡桃木
        self.assertNotIn('柜门 胡桃饰面', library['materials'])
        # 同一张图在另一个任务里又用了：并进同一张，同材质的框换成新的，别的材质保留
        other = self.studio.create({'product_dir': str(self.product)})['id']
        again = self.calibration.upload_ref(other, '色卡.png', self.content)
        self.calibration.change_ref(other, again['id'], {'regions': [region('摩卡红橡', 'library', [.3, .3, .2, .2]),
                                                                     region('样板橙', 'new')]})
        merged, = self.library.read()['annotated']
        self.assertEqual(merged['file'], entry['file'])
        self.assertEqual([(r['material'], r['box']) for r in merged['regions']],
                         [('胡桃木', [.6, .1, .2, .5]), ('摩卡红橡', [.3, .3, .2, .2]), ('样板橙', [.05, .1, .2, .5])])
        self.assertIn('样板橙', self.library.read()['materials'])
        for index in range(3):
            self.assertTrue((self.root/annotation_crop(merged['file'], index)).is_file())
        # 整体参考（没有框）不收
        self.update(self.upload('整体.png'), [])
        self.assertEqual(len(self.library.read()['annotated']), 1)
        self.assertEqual(first['library_file'], entry['file'])

    def test_picking_library_annotation_reuses_it(self):
        saved = self.library.save_annotation([region('胡桃木', 'library')], source='库里的图.png', content=self.content)
        picked = self.calibration.upload_ref(self.id, '库里的图.png', b'x', library_file=saved['file'])
        self.assertEqual(picked['library_file'], saved['file'])
        self.assertEqual((self.folder/picked['file']).read_bytes(), (self.root/saved['file']).read_bytes())  # 原样拷过来
        self.calibration.change_ref(self.id, picked['id'], {'regions': [region('胡桃木', 'library', [.4, .4, .2, .2]),
                                                                        region('奶油白', 'library')]})
        entry, = self.library.read()['annotated']
        self.assertEqual(entry['file'], saved['file'])
        self.assertEqual([(r['material'], r['box']) for r in entry['regions']],
                         [('胡桃木', [.4, .4, .2, .2]), ('奶油白', [.05, .1, .2, .5])])
        with self.assertRaises(StudioError):
            self.calibration.upload_ref(self.id, 'x.png', b'x', library_file='refs/annotated/../../config.json')

    def test_old_task_refs_are_synced_once(self):
        entry = self.upload()
        task = self.studio.load(self.id)
        old = task['ai_calibration']['task_refs'][0]
        old.update(in_library=False, regions=[region('胡桃木', 'library'), region('长虹玻璃', 'library', [.5, .1, .3, .3])])
        old.pop('library_synced', None)
        self.studio.save(task)
        self.assertEqual(self.library.read()['annotated'], [])
        self.assertEqual(self.calibration.sync_library(), 1)
        library_entry, = self.library.read()['annotated']
        self.assertEqual([r['material'] for r in library_entry['regions']], ['胡桃木', '长虹玻璃'])
        synced = self.studio.load(self.id)['ai_calibration']['task_refs'][0]
        self.assertEqual((synced['library_synced'], synced['library_file']), (True, library_entry['file']))
        self.assertEqual(self.calibration.sync_library(), 0)                       # 已经收过的不再处理
        self.assertEqual(len(self.library.read()['annotated']), 1)
        self.assertEqual(entry['id'], synced['id'])

    def test_cleanup_removes_task_ref_files_and_entries(self):
        self.update(self.upload(), [region('摩卡红橡', 'library')], True)
        task = self.studio.load(self.id)
        task['delivered'] = {'at': now()}
        self.studio.save(task)
        self.assertEqual(ExchangeCleanup(self.studio).clean()['cleaned_tasks'], 1)
        self.assertFalse((self.folder/'calibration_refs').exists())
        self.assertEqual(self.studio.describe(self.id)['task']['ai_calibration']['task_refs'], [])
        self.assertTrue((self.root/self.library.read()['annotated'][0]['file']).is_file())

    def test_http_methods_and_origin(self):
        base = f'/api/tasks/{self.id}/calibration-refs/'

        def request(method, suffix, content=b'', origin=None):
            handler = object.__new__(handler_for(self.studio))
            handler.path, handler.command = base+suffix, method
            handler.server = SimpleNamespace(server_port=8796)
            handler.rfile = io.BytesIO(content)
            handler.headers = {'Host': '127.0.0.1:8796', 'Content-Length': str(len(content)),
                               'Content-Type': 'application/json', 'Origin': origin}
            handler.reply = Mock()
            getattr(handler, 'do_'+method)()
            return handler.reply.call_args.args

        upload = 'upload?name=ref.png'
        self.assertEqual(request('POST', upload, self.content, 'https://example.com')[1], 403)
        result, status = request('POST', upload, self.content)
        self.assertEqual(status, 201)
        ref_id = result['entry']['id']
        edit = json.dumps({'regions': [region('摩卡红橡', 'library')], 'in_library': False}).encode()
        self.assertEqual(request('PUT', ref_id, edit, 'https://example.com')[1], 403)
        self.assertEqual(request('DELETE', ref_id, origin='https://example.com')[1], 403)
        self.assertEqual(len(request('PUT', ref_id, edit)[0]['entry']['regions']), 1)
        self.assertTrue(request('DELETE', ref_id)[0]['deleted'])
        self.assertEqual(request('DELETE', ref_id)[1], 404)
