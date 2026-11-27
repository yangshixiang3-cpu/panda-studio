"""1.7.2 清单增删排序保留成果、撤销删除恢复文件、作业回写对齐与 /api/file 缩略图测试。"""
import copy
import io
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.app import LocalServer, handler_for
from server.core import (REMOVED_KEEP, THUMB_KEEP_DAYS, THUMB_MAX, THUMB_MIN,
                         Studio, StudioError, atomic_json, inside, read_json)
from test_temp_dirs import test_directory


def png_bytes(size, color):
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, format='PNG')
    return buf.getvalue()


class ItemsAndThumbs172Tests(unittest.TestCase):
    def setUp(self):
        self.root = test_directory(self, 'items172-')
        config = read_json(ROOT / 'config.json')
        config['calibration_auto'] = False
        self.studio = Studio(self.root, config)
        self.addCleanup(self.studio.calibration.close)
        self.product = self.root / '测试产品'
        (self.product / '模型').mkdir(parents=True)
        (self.product / '参考图').mkdir()
        (self.product / 'sku').mkdir()
        (self.product / '模型' / '柜子.max').write_text('max', encoding='utf-8')
        self.task = self.studio.create({'product_dir': str(self.product)})

    def _add_views(self, labels=(('正面', 'front'), ('侧面', 'side'))):
        task = self.studio.load(self.task['id'])
        items = list(task['items'])
        base = items[0]
        for label, view in labels:
            entry = copy.deepcopy(base)
            entry.pop('item_id', None)
            entry['no'] = len(items) + 1
            entry['label'] = label
            entry['view'] = view
            items.append(entry)
        return self.studio.update(task['id'], {'items': items})

    def _finish_mode(self, mode, tag_prefix):
        job = self.studio.submit(self.task['id'], {'mode': mode})
        for entry in job['items']:
            no = entry['no']
            out = Path(entry['out_image'])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(f'{tag_prefix}:{mode}:{no}'.encode())
            if entry.get('out_mask'):
                mask = Path(entry['out_mask'])
                mask.parent.mkdir(parents=True, exist_ok=True)
                mask.write_bytes(f'{tag_prefix}:mask:{no}'.encode())
        atomic_json(self.studio.exchange / 'jobs' / f"{job['job_id']}.status.json", {
            'job_id': job['job_id'], 'state': 'done',
            'items': [{'no': i['no'], 'state': 'done', 'message': ''} for i in job['items']],
        })
        return job, self.studio.merge(self.task['id'])['task']

    def test_add_view_preserves_existing_results_and_calibration(self):
        self._add_views((('正面', 'front'),))
        small_job, _ = self._finish_mode('smallshot', 's')
        final_job, task = self._finish_mode('final', 'f')
        task['ai_calibration']['accepted_round'] = 1
        task['ai_calibration']['accepted_overrides'] = {
            'materials': {'木': {'brightness_mul': 1.05}},
            'items': {'1': {'lights': {'rotate_deg': 15}}, '2': {'lights': {'rotate_deg': -10}}},
        }
        self.studio.save(task)

        new_item = copy.deepcopy(task['items'][0])
        new_item.pop('item_id')
        new_item.update(no=3, label='俯视', view='top')
        updated = self.studio.update(task['id'], {'items': [*task['items'], new_item]})

        self.assertEqual([i['no'] for i in updated['items']], [1, 2, 3])
        for idx, no in ((0, 1), (1, 2)):
            self.assertEqual(updated['items'][idx]['smallshot']['state'], 'done')
            self.assertEqual(updated['items'][idx]['smallshot']['job_id'], small_job['job_id'])
            self.assertEqual(updated['items'][idx]['smallshot']['file'].replace('\\', '/'), f'smallshots/{no}.jpg')
            self.assertEqual(updated['items'][idx]['final']['state'], 'done')
            self.assertEqual(updated['items'][idx]['final']['job_id'], final_job['job_id'])
            self.assertEqual(updated['items'][idx]['final']['file'].replace('\\', '/'), f'final/{no}.jpg')
        self.assertEqual(updated['items'][2]['smallshot']['state'], 'none')
        self.assertEqual(updated['items'][2]['final']['state'], 'none')
        # 加视角不应误清空 AI 校准认可结果
        self.assertEqual(updated['ai_calibration']['accepted_round'], 1)
        self.assertEqual(updated['ai_calibration']['accepted_overrides']['items'],
                         {'1': {'lights': {'rotate_deg': 15}}, '2': {'lights': {'rotate_deg': -10}}})

    def test_reorder_swaps_files_atomically_updates_overrides_and_delivers_new_order(self):
        self._add_views((('正面', 'front'), ('侧面', 'side')))
        self.studio.update(self.task['id'], {'lighting': {'enabled': True, 'azimuth_deg': -45}})
        self._finish_mode('smallshot', 'orig')
        _, task = self._finish_mode('final', 'orig')
        folder = self.studio.task_dir(task['id'])
        (folder / 'lighting').mkdir(exist_ok=True)
        for no in (1, 2, 3):
            (folder / 'lighting' / f'{no}.png').write_bytes(f'light:{no}'.encode())
            (folder / 'lighting' / f'{no}.png.prev').write_bytes(f'light-prev:{no}'.encode())
        task['ai_calibration']['accepted_overrides'] = {
            'items': {'1': {'lights': {'rotate_deg': 10}}, '3': {'lights': {'rotate_deg': -20}}},
        }
        self.studio.save(task)

        # 反转顺序：3 -> 1, 2 -> 2, 1 -> 3（编号 1 和 3 直接互换，验证原子改名不互相覆盖）
        reordered = [copy.deepcopy(task['items'][2]), copy.deepcopy(task['items'][1]), copy.deepcopy(task['items'][0])]
        updated = self.studio.update(task['id'], {'items': reordered})

        self.assertEqual([i['label'] for i in updated['items']], ['侧面', '正面', '主图'])
        self.assertEqual([i['no'] for i in updated['items']], [1, 2, 3])
        self.assertTrue(all(i['smallshot']['state'] == 'done' and i['final']['state'] == 'done' for i in updated['items']))
        self.assertEqual((folder / 'smallshots' / '1.jpg').read_bytes(), b'orig:smallshot:3')
        self.assertEqual((folder / 'smallshots' / '2.jpg').read_bytes(), b'orig:smallshot:2')
        self.assertEqual((folder / 'smallshots' / '3.jpg').read_bytes(), b'orig:smallshot:1')
        self.assertEqual((folder / 'final' / '1.jpg').read_bytes(), b'orig:final:3')
        self.assertEqual((folder / 'final' / '1_遮罩_ID.jpg').read_bytes(), b'orig:mask:3')
        self.assertEqual((folder / 'final' / '3.jpg').read_bytes(), b'orig:final:1')
        self.assertEqual((folder / 'final' / '3_遮罩_ID.jpg').read_bytes(), b'orig:mask:1')
        self.assertEqual((folder / 'lighting' / '1.png').read_bytes(), b'light:3')
        self.assertEqual((folder / 'lighting' / '3.png').read_bytes(), b'light:1')
        self.assertEqual((folder / 'lighting' / '1.png.prev').read_bytes(), b'light-prev:3')
        self.assertEqual((folder / 'lighting' / '3.png.prev').read_bytes(), b'light-prev:1')
        # accepted_overrides 里的条目编号也跟着互换
        self.assertEqual(updated['ai_calibration']['accepted_overrides']['items'],
                         {'1': {'lights': {'rotate_deg': -20}}, '3': {'lights': {'rotate_deg': 10}}})

        # 入库文件按新顺序命名且内容与新顺序一致
        plan = self.studio.delivery_plan(task['id'])
        self.studio.deliver(task['id'], plan['token'])
        sku = self.product / 'sku'
        self.assertEqual((sku / '1.jpg').read_bytes(), b'orig:final:3')
        self.assertEqual((sku / '1_遮罩_ID.jpg').read_bytes(), b'orig:mask:3')
        self.assertEqual((sku / '2.jpg').read_bytes(), b'orig:final:2')
        self.assertEqual((sku / '3.jpg').read_bytes(), b'orig:final:1')
        self.assertEqual((sku / '3_遮罩_ID.jpg').read_bytes(), b'orig:mask:1')
        self.assertEqual((self.product / '光向参考' / '1.png').read_bytes(), b'light:3')
        self.assertEqual((self.product / '光向参考' / '3.png').read_bytes(), b'light:1')
        manifest = read_json(sku / '白底清单.json')
        self.assertEqual([(r['no'], r['label']) for r in manifest], [(1, '侧面'), (2, '正面'), (3, '主图')])

    def test_merge_resolves_job_item_after_renumber(self):
        # 只有第 1 项在作业里完成、第 2 项在同作业里失败；互换并再次改序后 merge 仍按原 job_item 准确对应。
        self._add_views((('正面', 'front'), ('侧面', 'side')))
        task = self.studio.load(self.task['id'])
        task['items'][2]['enabled'] = False
        self.studio.update(task['id'], {'items': task['items']})

        job = self.studio.submit(task['id'], {'mode': 'final'})
        status_path = self.studio.exchange / 'jobs' / f"{job['job_id']}.status.json"
        atomic_json(status_path, {
            'job_id': job['job_id'], 'state': 'failed', 'message': '明细失败',
            'items': [{'no': 1, 'state': 'done', 'message': ''},
                      {'no': 2, 'state': 'failed', 'message': '视角2失败'}],
        })
        task = self.studio.merge(task['id'])['task']
        self.assertEqual([i['final']['state'] for i in task['items']], ['done', 'failed', 'none'])

        # 第一次重排：[正面(原2), 侧面(原3), 主图(原1)]
        moved1 = [task['items'][1], task['items'][2], task['items'][0]]
        updated = self.studio.update(task['id'], {'items': moved1})
        self.assertEqual(updated['items'][0]['final']['job_item'], {'job_id': job['job_id'], 'no': 2})
        self.assertEqual(updated['items'][2]['final']['job_item'], {'job_id': job['job_id'], 'no': 1})

        # 再次重排：[侧面(原3), 主图(原1), 正面(原2)]，原 job_item.no 不能被覆盖成中间编号
        moved2 = [updated['items'][1], updated['items'][2], updated['items'][0]]
        updated2 = self.studio.update(task['id'], {'items': moved2})
        self.assertEqual(updated2['items'][1]['final']['job_item'], {'job_id': job['job_id'], 'no': 1})
        self.assertEqual(updated2['items'][2]['final']['job_item'], {'job_id': job['job_id'], 'no': 2})

        merged = self.studio.merge(task['id'])['task']
        self.assertEqual([i['final']['state'] for i in merged['items']], ['none', 'done', 'failed'])
        self.assertEqual(merged['items'][2]['final']['message'], '视角2失败')

        # 如果校准小样文件在 calibration/round-1/smallshots/1.jpg，改序后路径保持不变且 merge 仍保留 done
        cal_file = self.studio.task_dir(task['id']) / 'calibration' / 'round-1' / 'smallshots' / '1.jpg'
        cal_file.parent.mkdir(parents=True, exist_ok=True)
        cal_file.write_bytes(b'cal-shot')
        cal_job = {'type': 'render', 'job_id': '20261126-120000-cal1', 'task_id': task['id'],
                   'created_at': task['created_at'], 'mode': 'smallshot', 'items': [{'no': 1}]}
        atomic_json(self.studio.exchange / 'jobs' / f"{cal_job['job_id']}.json", cal_job)
        atomic_json(self.studio.exchange / 'jobs' / f"{cal_job['job_id']}.status.json",
                    {'state': 'done', 'items': [{'no': 1, 'state': 'done'}]})
        t = self.studio.load(task['id'])
        t['items'][0]['smallshot'] = {'state': 'done', 'file': 'calibration/round-1/smallshots/1.jpg',
                                      'job_id': cal_job['job_id']}
        self.studio.save(t)
        swapped = self.studio.update(task['id'], {'items': [t['items'][1], t['items'][0], t['items'][2]]})
        self.assertEqual(swapped['items'][1]['smallshot']['file'], 'calibration/round-1/smallshots/1.jpg')
        self.assertEqual(self.studio.merge(task['id'])['task']['items'][1]['smallshot']['state'], 'done')

    def test_delete_stashes_files_and_undo_restores_item_and_files(self):
        self._add_views((('正面', 'front'), ('侧面', 'side')))
        self._finish_mode('smallshot', 'd')
        _, task = self._finish_mode('final', 'd')
        folder = self.studio.task_dir(task['id'])
        deleted = copy.deepcopy(task['items'][1])  # 第 2 张「正面」
        deleted_id = deleted['item_id']

        # 删除第 2 张
        after_del = self.studio.update(task['id'], {'items': [task['items'][0], task['items'][2]]})
        self.assertEqual([i['label'] for i in after_del['items']], ['主图', '侧面'])
        self.assertEqual((folder / 'final' / '1.jpg').read_bytes(), b'd:final:1')
        self.assertEqual((folder / 'final' / '2.jpg').read_bytes(), b'd:final:3')
        self.assertFalse((folder / 'final' / '3.jpg').exists())
        self.assertEqual((folder / 'removed' / deleted_id / 'final.jpg').read_bytes(), b'd:final:2')
        self.assertEqual((folder / 'removed' / deleted_id / 'final_遮罩_ID.jpg').read_bytes(), b'd:mask:2')
        self.assertEqual((folder / 'removed' / deleted_id / 'smallshots.jpg').read_bytes(), b'd:smallshot:2')

        # 撤销删除：带原 item_id 插回原位置
        restored = self.studio.update(task['id'], {'items': [after_del['items'][0], deleted, after_del['items'][1]]})
        self.assertEqual([i['label'] for i in restored['items']], ['主图', '正面', '侧面'])
        self.assertEqual([i['final']['state'] for i in restored['items']], ['done', 'done', 'done'])
        self.assertEqual([i['smallshot']['state'] for i in restored['items']], ['done', 'done', 'done'])
        self.assertEqual((folder / 'final' / '1.jpg').read_bytes(), b'd:final:1')
        self.assertEqual((folder / 'final' / '2.jpg').read_bytes(), b'd:final:2')
        self.assertEqual((folder / 'final' / '2_遮罩_ID.jpg').read_bytes(), b'd:mask:2')
        self.assertEqual((folder / 'final' / '3.jpg').read_bytes(), b'd:final:3')
        self.assertEqual((folder / 'smallshots' / '2.jpg').read_bytes(), b'd:smallshot:2')
        self.assertFalse((folder / 'removed' / deleted_id).exists())

        # 删除后如果更改了打光再撤销，恢复的条目应自动失效重算
        after_del2 = self.studio.update(task['id'], {'items': [restored['items'][0], restored['items'][2]]})
        self.studio.update(task['id'], {'lighting': {'enabled': True, 'azimuth_deg': 45}})
        after_light = self.studio.load(task['id'])
        restored_lit = self.studio.update(task['id'], {'items': [after_light['items'][0], restored['items'][1], after_light['items'][1]]})
        self.assertEqual(restored_lit['items'][1]['final']['state'], 'none')
        self.assertEqual(restored_lit['items'][1]['smallshot']['state'], 'none')

    def test_removed_stash_prunes_beyond_limit(self):
        labels = [(f'视角{i}', 'custom') for i in range(2, REMOVED_KEEP + 4)]
        task = self._add_views(labels)
        folder = self.studio.task_dir(task['id'])
        (folder / 'final').mkdir(exist_ok=True)
        for item in task['items']:
            (folder / 'final' / f"{item['no']}.jpg").write_bytes(f"img-{item['no']}".encode())
        # 一次性删掉超过 REMOVED_KEEP 个条目，只保留最近 REMOVED_KEEP 个暂存目录
        kept = self.studio.update(task['id'], {'items': [task['items'][0]]})
        self.assertEqual(len(kept['removed_items']), REMOVED_KEEP)
        removed_dirs = [p for p in (folder / 'removed').iterdir() if p.is_dir()]
        self.assertEqual(len(removed_dirs), REMOVED_KEEP)

    def test_api_file_thumbnails_cache_clamp_exif_fallback_and_purge(self):
        ref_dir = self.product / '参考图'
        wide = ref_dir / 'wide.png'
        wide.write_bytes(png_bytes((1600, 1200), (40, 120, 200)))
        bad = ref_dir / 'corrupt.png'
        bad.write_bytes(b'not a valid png')
        non_img = self.studio.task_dir(self.task['id']) / 'note.txt'
        non_img.write_text('hello', encoding='utf-8')
        # EXIF Orientation=6（90° 顺时针旋转）：原始 600x400 转正后应为 400x600 竖图
        rotated = ref_dir / 'exif.jpg'
        exif = Image.Exif()
        exif[0x0112] = 6
        Image.new('RGB', (600, 400), (200, 80, 50)).save(rotated, format='JPEG', exif=exif)

        server = LocalServer(('127.0.0.1', 0), handler_for(self.studio))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'

        def fetch(path, w=None):
            params = {'path': str(path), 'task_id': self.task['id']}
            if w is not None:
                params['w'] = str(w)
            with urllib.request.urlopen(base + '/api/file?' + urllib.parse.urlencode(params), timeout=5) as resp:
                return resp.headers.get('Content-Type', ''), resp.read()

        # 1. w=200 返回 WEBP，宽 <= 200，保持 4:3 比例，缓存在 exchange/.thumbs（不在 C:）
        ctype, data = fetch(wide, 200)
        self.assertEqual(ctype, 'image/webp')
        with Image.open(io.BytesIO(data)) as im:
            self.assertEqual(im.format, 'WEBP')
            self.assertEqual(im.size, (200, 150))
        thumbs = list((self.studio.exchange / '.thumbs').glob('*.webp'))
        self.assertEqual(len(thumbs), 1)
        self.assertTrue(inside(thumbs[0], self.studio.exchange / '.thumbs'))

        # 2. 超大 w 被限到 THUMB_MAX (1024)，过小 w 被限到 THUMB_MIN (32)
        _, big_data = fetch(wide, 99999)
        with Image.open(io.BytesIO(big_data)) as im:
            self.assertEqual(im.format, 'WEBP')
            self.assertEqual(im.size, (THUMB_MAX, 768))
        _, tiny_data = fetch(wide, 5)
        with Image.open(io.BytesIO(tiny_data)) as im:
            self.assertEqual(im.format, 'WEBP')
            self.assertEqual(im.size, (THUMB_MIN, 24))

        # 3. EXIF 旋转生效：600x400 + Orientation=6 -> 竖图 133x200
        _, rot_data = fetch(rotated, 200)
        with Image.open(io.BytesIO(rot_data)) as im:
            self.assertLess(im.width, im.height)
            self.assertLessEqual(max(im.size), 200)

        # 4. 损坏图片和非图片平稳回退到原文件
        _, bad_data = fetch(bad, 200)
        self.assertEqual(bad_data, b'not a valid png')
        _, txt_data = fetch(non_img, 200)
        self.assertEqual(txt_data, b'hello')

        # 5. 权限校验仍然有效：不允许读取白名单外路径
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(
                base + '/api/file?' + urllib.parse.urlencode({'path': 'C:\\Windows\\win.ini', 'task_id': self.task['id'], 'w': '200'}),
                timeout=5)
        self.assertEqual(err.exception.code, 403)

        # 6. 过期缩略图清理（超过 THUMB_KEEP_DAYS 天）
        stale = thumbs[0]
        old_ts = time.time() - (THUMB_KEEP_DAYS + 2) * 86400
        os.utime(stale, (old_ts, old_ts))
        self.assertEqual(self.studio.purge_thumbs(), 1)
        self.assertFalse(stale.exists())


if __name__ == '__main__':
    unittest.main()
