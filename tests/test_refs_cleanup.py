"""参考图标注、选图和交换目录清理的隔离验收。"""
import io
import json
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image, ImageStat
from test_temp_dirs import test_directory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.app import handler_for
from server.calibration import select_images
from server.cleanup import DAY, OUTPUT_DIRS, ExchangeCleanup
from server.color_stats import srgb_to_lab
from server.core import Studio, StudioError, atomic_json, read_json
from server.ref_library import annotation_crop, material_key


class AnnotationCleanupTests(unittest.TestCase):
    def setUp(self):
        self.root = test_directory(self, 'refs-cleanup-')
        self.studio = Studio(self.root, {'exchange_dir': 'exchange', 'calibration_auto': False})
        self.addCleanup(self.studio.calibration.close)
        self.product = self.root/'产品'
        self.product.mkdir()
        (self.product/'柜.max').touch()
        self.refs = self.studio.refs
        image = Image.new('RGB', (3000, 1000), (180, 45, 25))
        image.paste((35, 110, 195), (1500, 0, 3000, 1000))
        self.source = self.root/'色块.png'
        image.save(self.source)
        self.content = self.source.read_bytes()

    def verify_crop(self, entry, index, dimensions, rgb):
        path = self.root/annotation_crop(entry['file'], index)
        with Image.open(path) as image:
            self.assertEqual(image.size, dimensions)
            for value, target in zip(ImageStat.Stat(image).mean, rgb):
                self.assertAlmostEqual(value, target, delta=2)

    def test_annotation_save_read_update_remove_region_and_delete(self):
        red = {'material': '摩卡红橡', 'box': [.1, .2, .3, .5]}
        blue = {'material': '幻影灰', 'box': [.6, .1, .3, .6]}
        entry = self.refs.save_annotation([red, blue], source=str(self.source), content=self.content)
        file = entry['file']
        self.assertEqual(self.refs.read()['annotated'], [entry])
        with Image.open(self.root/file) as image:
            self.assertEqual(image.size, (2400, 800))
        self.verify_crop(entry, 0, (720, 400), (180, 45, 25))
        self.verify_crop(entry, 1, (720, 480), (35, 110, 195))
        with self.assertRaises(StudioError):
            self.refs.save_annotation([dict(red, box=[.9, 0, .4, .5])], file=file)
        moved = {'material': '北欧橡木', 'box': [.7, .2, .2, .4]}
        updated = self.refs.save_annotation([moved], file=file)
        self.assertEqual(updated, dict(entry, regions=[moved]))
        self.assertEqual(self.refs.read()['annotated'], [updated])
        self.verify_crop(updated, 0, (480, 320), (35, 110, 195))
        self.assertFalse((self.root/annotation_crop(file, 1)).exists())
        self.refs.delete_annotation(file)
        self.assertEqual(self.refs.read()['annotated'], [])
        self.assertFalse((self.root/file).exists())
        self.assertFalse((self.root/annotation_crop(file, 0)).exists())
        self.assertEqual(self.source.read_bytes(), self.content)
        plain = self.refs.save_annotation([], source=self.source.name, content=self.content)
        self.assertEqual(self.refs.read()['annotated'], [plain])

    def test_material_keywords_create_and_prefer_longest(self):
        name = '深色胡桃'
        library = self.refs.change_material(name, create=True)
        self.assertEqual(library['materials'][name]['keywords'], [name])
        self.assertEqual(material_key('Max 深色胡桃 清漆', library), name)
        library = self.refs.change_material(name, [' 墨色胡桃 ', 'WALNUT DR'], create=False)
        self.assertEqual(material_key('柜门 walnut dr', library), name)
        self.assertEqual(material_key('Max 深色胡桃 清漆', library), '胡桃木')
        for invalid in ('style', 'annotated', '..', 'A/目录'):
            with self.assertRaises(StudioError):
                self.refs.change_material(invalid, create=True)

    def test_calibration_uses_annotated_then_approved_with_lab(self):
        key = '摩卡红橡'
        task = self.studio.create({'product_dir': str(self.product)})
        library = self.refs.read()
        fallback = self.refs.add(key, '已认可.png', self.content, library=library)
        fallback['status'] = 'approved'
        self.refs.save(library)
        red = {'material': key, 'box': [.1, .2, .2, .4]}
        blue = {'material': key, 'box': [.7, .2, .2, .4]}
        entry = self.refs.save_annotation([red], source=self.source.name, content=self.content)
        spec = (self.studio, task, {'n': 1}, {'materials': [{'name': '柜门 摩卡红橡'}]}, {'items': []})
        images, roles = select_images(*spec)
        self.assertEqual([p.name for p in images], [Path(annotation_crop(entry['file'], 0)).name, Path(fallback['file']).name])
        with Image.open(images[0]) as selected:
            expected = ', '.join(f'{v:.1f}' for v in srgb_to_lab(selected.getpixel((0, 0))))
        self.assertEqual(roles[0]['role'], f'{key} 用户标注参考 Lab≈({expected})')
        self.assertIn('已认可', roles[1]['role'])
        self.refs.save_annotation([red, blue, red], file=entry['file'])
        images, roles = select_images(*spec)
        self.assertEqual(len(images), 2)
        self.assertEqual([p.name for p in images], [Path(annotation_crop(entry['file'], i)).name for i in (0, 1)])
        labs = []
        for path in images:
            with Image.open(path) as selected:
                labs.append(srgb_to_lab(selected.getpixel((0, 0))))
        combined = ', '.join(f'{(a+b)/2:.1f}' for a, b in zip(*labs))
        self.assertEqual(roles[0]['role'], f'{key} 用户标注参考 Lab≈({combined})')

    def seed_task(self, *, days=None, state=None, calibration_state=None):
        task = self.studio.create({'product_dir': str(self.product)})
        folder = self.studio.task_dir(task['id'])
        if days is not None:
            task['delivered'] = {'at': (datetime.now(timezone.utc)-timedelta(days=days)).isoformat()}
        if calibration_state:
            task['ai_calibration']['state'] = calibration_state
        self.studio.save(task)
        for name in (*OUTPUT_DIRS, 'preview'):
            (folder/name).mkdir()
            (folder/name/'测试图.jpg').write_bytes(b'test')
        if state:
            job = {'job_id': task['id'], 'task_id': task['id'], 'created_at': task['created_at'], 'type': 'export_preview'}
            atomic_json(self.studio.exchange/'jobs'/f"{job['job_id']}.json", job)
            if state != 'queued':
                atomic_json(self.studio.exchange/'jobs'/f"{job['job_id']}.status.json", {'state': state})
        return folder

    def dispatch(self, path, data=None, origin=None):
        handler_class = handler_for(self.studio)
        handler = object.__new__(handler_class)
        handler.path = path
        handler.command = 'POST' if data is not None else 'GET'
        content = json.dumps(data).encode() if data is not None else b''
        handler.rfile = io.BytesIO(content)
        handler.server = SimpleNamespace(server_port=8796)
        handler.headers = {'Host': '127.0.0.1:8796', 'Origin': origin, 'Content-Type': 'application/json',
                           'Content-Length': str(len(content))}
        handler.reply = Mock()
        handler.safe_dispatch()
        return handler.reply.call_args.args

    def test_cleanup_ages_jobs_and_manual_api(self):
        old = self.seed_task(days=31, state='done')
        recent = self.seed_task(days=29)
        pending = self.seed_task()
        queued = self.seed_task(days=40, state='queued')
        rendering = self.seed_task(days=41, state='running')
        thinking = self.seed_task(days=41, calibration_state='thinking')
        sku = self.product/'sku'
        sku.mkdir()
        (sku/'成品.jpg').write_bytes(b'final')
        output_bytes = 4*len(OUTPUT_DIRS)
        self.assertEqual(ExchangeCleanup(self.studio).clean(before=time.time()-30*DAY),
                         {'cleaned_tasks': 1, 'cleaned_bytes': output_bytes})
        for name in OUTPUT_DIRS:
            self.assertFalse((old/name).exists())
        self.assertTrue((old/'preview'/'测试图.jpg').is_file())
        self.assertIn('cleaned_at', read_json(old/'task.json'))
        storage, = self.dispatch('/api/storage')
        self.assertEqual((storage['tasks'], storage['cleanable_tasks'], storage['cleanable_bytes']), (6, 1, output_bytes))
        self.assertEqual(self.dispatch('/api/storage/cleanup', {'mode': 'delivered'}, 'https://example.com')[1], 403)
        self.assertEqual(self.dispatch('/api/storage/cleanup', {'mode': 'all'})[1], 400)
        result, = self.dispatch('/api/storage/cleanup', {'mode': 'delivered'})
        self.assertEqual(result, {'cleaned_tasks': 1, 'cleaned_bytes': output_bytes})
        self.assertIn('cleaned_at', read_json(recent/'task.json'))
        self.assertEqual(self.dispatch('/api/storage/cleanup', {'mode': 'delivered'})[0]['cleaned_tasks'], 0)
        for task in (pending, queued, rendering, thinking):
            self.assertNotIn('cleaned_at', read_json(task/'task.json'))
            for name in OUTPUT_DIRS:
                self.assertTrue((task/name/'测试图.jpg').is_file())
        self.assertEqual((sku/'成品.jpg').read_bytes(), b'final')

    def test_background_respects_configured_retention(self):
        self.studio.config['cleanup_delivered_after_days'] = 3
        expired = self.seed_task(days=4)
        kept = self.seed_task(days=2)
        worker = ExchangeCleanup(self.studio)
        worker.stopping.wait = Mock(side_effect=lambda delay: worker.stopping.set())
        worker.start()
        worker.thread.join(timeout=10)
        self.addCleanup(worker.close)
        self.assertIn('cleaned_at', read_json(expired/'task.json'))
        self.assertNotIn('cleaned_at', read_json(kept/'task.json'))
        worker.stopping.wait.assert_called_once_with(DAY)

    def test_keeps_final_rendered_after_last_delivery(self):
        folder = self.seed_task(days=31)
        task = read_json(folder/'task.json')
        job = {'job_id': task['id'], 'task_id': task['id'], 'created_at': task['created_at'],
               'type': 'render', 'mode': 'final', 'items': []}
        atomic_json(self.studio.exchange/'jobs'/f"{job['job_id']}.json", job)
        atomic_json(self.studio.exchange/'jobs'/f"{job['job_id']}.status.json", {'state': 'done'})
        cleanup = ExchangeCleanup(self.studio)
        self.assertEqual(cleanup.storage()['cleanable_tasks'], 0)
        self.assertEqual(cleanup.clean()['cleaned_tasks'], 0)
        self.assertTrue((folder/'final'/'测试图.jpg').is_file())
