"""1.8.0 后端稳定与流畅优化及材质参数路由单测；所有临时文件仅写入 tests/tmp。"""
import base64
from datetime import datetime, timedelta, timezone
import http.client
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

from server import app as server_app
from server import cleanup as cleanup_mod
from server import core as core_mod
from server import node_setup
from server.app import LocalServer, handler_for
from server.cleanup import DAY, ExchangeCleanup
from server.core import Studio, atomic_json, read_json
from server.jobs import JobIndex
from server.runner import PROBE_TTL_SEC, RunnerStatus
from test_temp_dirs import test_directory

def _make_png_bytes():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (2, 2), (255, 255, 255)).save(buf, 'PNG')
    return buf.getvalue()


PNG_1X1 = _make_png_bytes()

_desktop_loader = importlib.machinery.SourceFileLoader('whitebg_desktop_perf', str(ROOT / 'desktop.pyw'))
_desktop_spec = importlib.util.spec_from_loader(_desktop_loader.name, _desktop_loader)
desktop = importlib.util.module_from_spec(_desktop_spec)
_desktop_loader.exec_module(desktop)


def _iso_days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='microseconds')


def _make_studio_root(testcase, prefix='perf180-'):
    root = test_directory(testcase, prefix)
    cfg = read_json(ROOT / 'config.json')
    atomic_json(root / 'config.json', cfg)
    (root / 'VERSION').write_text('1.8.0-test\n', encoding='utf-8')
    (root / 'template').mkdir(parents=True, exist_ok=True)
    (root / 'template' / '白底模板.max').write_bytes(b'template')
    rps = cfg.get('render_preset_rps')
    if rps:
        rps_path = root / rps
        rps_path.parent.mkdir(parents=True, exist_ok=True)
        rps_path.write_bytes(b'rps')
    product = root / '产品A'
    (product / '模型').mkdir(parents=True, exist_ok=True)
    (product / '模型' / '柜子.max').write_bytes(b'max-model')
    return root, product


class LockScopeAndTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.root, self.product = _make_studio_root(self, 'lock-')
        self.studio = Studio(self.root)
        self.addCleanup(self.studio.calibration.close)
        self.handler_cls = handler_for(self.studio)
        self.server = LocalServer(('127.0.0.1', 0), self.handler_cls)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.port = self.server.server_port
        self.base = f'http://127.0.0.1:{self.port}'

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def _get_json(self, path, timeout=2.0):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())

    def _request_json(self, method, path, payload, timeout=5.0):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(
            self.base + path,
            data=body,
            method=method,
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())

    def test_handler_timeout_is_30s(self):
        self.assertEqual(self.handler_cls.timeout, 30)

    def test_slow_request_body_does_not_block_health_or_tasks(self):
        task = self.studio.create({'product_dir': str(self.product)})
        body_bytes = json.dumps({'lighting': None}).encode('utf-8')
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.putrequest('PUT', f'/api/tasks/{task["id"]}')
        conn.putheader('Host', f'127.0.0.1:{self.port}')
        conn.putheader('Content-Type', 'application/json')
        conn.putheader('Content-Length', str(len(body_bytes)))
        conn.endheaders()
        # 只发前 2 字节，模拟慢速客户端卡在 self.body() 读取流中
        conn.send(body_bytes[:2])
        time.sleep(0.08)

        t0 = time.monotonic()
        status_health, health = self._get_json('/api/health', timeout=1.5)
        status_tasks, tasks = self._get_json('/api/tasks', timeout=1.5)
        elapsed = time.monotonic() - t0
        self.assertEqual(status_health, 200)
        self.assertEqual(health['service'], 'whitebg-studio')
        self.assertEqual(status_tasks, 200)
        self.assertEqual(len(tasks['tasks']), 1)
        self.assertLess(elapsed, 1.0)

        # 发完剩余请求体，PUT 请求正常完成
        conn.send(body_bytes[2:])
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        resp.read()

    def test_slow_scan_and_deliver_do_not_block_concurrent_reads(self):
        task = self.studio.create({'product_dir': str(self.product)})
        task_folder = self.studio.task_dir(task['id'])
        (task_folder / 'final').mkdir(parents=True, exist_ok=True)
        (task_folder / 'final' / '1.jpg').write_bytes(PNG_1X1)
        (task_folder / 'final' / '1_遮罩_ID.jpg').write_bytes(PNG_1X1)
        loaded = self.studio.load(task['id'])
        loaded['items'][0]['final']['state'] = 'done'
        self.studio.save(loaded)

        # 1) 慢速 scan 进行中，并发健康检查与任务读取仍秒回
        entered_scan = threading.Event()
        release_scan = threading.Event()
        real_walk = os.walk

        def slow_walk(top, *args, **kwargs):
            if Path(top) == self.product.resolve():
                entered_scan.set()
                release_scan.wait(timeout=3)
            return real_walk(top, *args, **kwargs)

        scan_result = []
        with patch('server.core.os.walk', side_effect=slow_walk):
            scan_worker = threading.Thread(
                target=lambda: scan_result.append(self._request_json('POST', '/api/scan', {'product_dir': str(self.product)})),
                daemon=True,
            )
            scan_worker.start()
            self.assertTrue(entered_scan.wait(timeout=2))
            status_h, _ = self._get_json('/api/health', timeout=1.0)
            status_t, _ = self._get_json(f'/api/tasks/{task["id"]}', timeout=1.0)
            self.assertEqual((status_h, status_t), (200, 200))
            release_scan.set()
            scan_worker.join(timeout=3)
        self.assertEqual(scan_result[0][0], 200)

        # 2) 慢速 deliver 文件拷贝进行中，不占 studio.lock，并发健康检查秒回
        _, plan = self._request_json('POST', f'/api/tasks/{task["id"]}/delivery-plan', {})
        entered_copy = threading.Event()
        release_copy = threading.Event()
        real_copy2 = shutil.copy2

        def slow_copy2(src, dst, *args, **kwargs):
            if '.staging-' in str(dst) and not entered_copy.is_set():
                entered_copy.set()
                release_copy.wait(timeout=3)
            return real_copy2(src, dst, *args, **kwargs)

        deliver_result = []
        with patch('server.core.shutil.copy2', side_effect=slow_copy2):
            deliver_worker = threading.Thread(
                target=lambda: deliver_result.append(
                    self._request_json('POST', f'/api/tasks/{task["id"]}/deliver', {'token': plan['token']})
                ),
                daemon=True,
            )
            deliver_worker.start()
            self.assertTrue(entered_copy.wait(timeout=2))
            # 验证此时主线程可以直接获取 studio.lock，且 HTTP 健康检查不被阻塞
            acquired = self.studio.lock.acquire(blocking=False)
            if acquired:
                self.studio.lock.release()
            self.assertTrue(acquired, 'deliver() 拷贝大文件期间不应持有 studio.lock')
            status_h2, _ = self._get_json('/api/health', timeout=1.0)
            self.assertEqual(status_h2, 200)
            release_copy.set()
            deliver_worker.join(timeout=3)
        self.assertEqual(deliver_result[0][0], 200)


class ProbeAndTaskCacheTests(unittest.TestCase):
    def test_runner_probe_ttl_and_async_refresh(self):
        root, _ = _make_studio_root(self, 'runner-ttl-')
        runner = RunnerStatus(root, root / 'exchange', {})
        calls = []
        gate = threading.Event()

        def fake_probe():
            calls.append(len(calls) + 1)
            if len(calls) == 2:
                gate.wait(timeout=2)
                return True, ['Autodesk 3ds Max 2024'], 0
            return False, [], 0

        with patch.object(runner, '_probe_max', side_effect=fake_probe):
            # 首次冷缓存：同步探测 1 次
            self.assertFalse(runner.max_running())
            self.assertEqual(len(calls), 1)
            # TTL 内再次调用：直接走内存缓存，不触发子进程
            self.assertFalse(runner.max_running())
            self.assertEqual(len(calls), 1)

            # 令缓存过期：调用方立即拿到旧缓存返回而不阻塞，后台线程异步刷新
            runner._probe_until = time.monotonic() - 1.0
            t0 = time.monotonic()
            self.assertFalse(runner.max_running())
            self.assertLess(time.monotonic() - t0, 0.2)
            gate.set()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not runner.max_running():
                time.sleep(0.02)
            self.assertTrue(runner.max_running())
            self.assertGreaterEqual(len(calls), 2)
            self.assertGreater(PROBE_TTL_SEC, 0)

    def test_node_setup_firewall_ttl_cache(self):
        node_setup._firewall_cache.invalidate()
        self.addCleanup(node_setup._firewall_cache.invalidate)
        proc = subprocess.CompletedProcess(
            args=['netsh'],
            returncode=0,
            stdout=b'Profile: Domain,Private,Public\nLocalPort: 8775\nLocalPort: 8776\n',
            stderr=b'',
        )
        with patch('server.node_setup.os.name', 'nt'), \
                patch('server.node_setup.subprocess.run', return_value=proc) as mocked_run:
            self.assertTrue(node_setup.firewall_rule_exists())
            self.assertTrue(node_setup.firewall_rule_exists())
            self.assertEqual(mocked_run.call_count, 1)

    def test_cleanup_task_json_mtime_cache(self):
        root, product = _make_studio_root(self, 'task-cache-')
        studio = Studio(root)
        self.addCleanup(studio.calibration.close)
        task = studio.create({'product_dir': str(product)})
        cleanup = ExchangeCleanup(studio)

        # 第一次 list_tasks 读取并建缓存
        first = cleanup.list_tasks('')
        self.assertEqual(len(first), 1)

        # 文件未修改时，反复 list_tasks 与 storage 不再调用 read_json 解析 task.json
        with patch.object(cleanup_mod, 'read_json', wraps=read_json) as spy_read:
            second = cleanup.list_tasks('')
            storage = cleanup.storage()
            self.assertEqual(spy_read.call_count, 0)
        self.assertEqual(second[0]['id'], task['id'])
        self.assertEqual(storage['tasks'], 1)
        # 污染返回值不会破坏内部缓存
        second[0]['product_name'] = '被调用方篡改'
        self.assertEqual(cleanup.list_tasks('')[0]['product_name'], product.name)

        # 更新 task.json 后缓存自动失效并读到最新内容
        studio.update(task['id'], {'output_preset': '6K'})
        updated = cleanup.list_tasks('')
        self.assertEqual(updated[0]['output_preset'], '6K')


class JobArchiveAndIndexTests(unittest.TestCase):
    def setUp(self):
        self.root, self.product = _make_studio_root(self, 'archive-')
        self.studio = Studio(self.root)
        self.addCleanup(self.studio.calibration.close)
        self.cleanup = ExchangeCleanup(self.studio)
        self.jobs_dir = self.studio.exchange / 'jobs'

    def _write_job_pair(self, job_id, task_id, *, jtype='render', mode='smallshot', state='done', days_ago=10):
        created = _iso_days_ago(days_ago + 0.01)
        started = _iso_days_ago(days_ago + 0.005)
        updated = _iso_days_ago(days_ago)
        job = {
            'job_id': job_id,
            'task_id': task_id,
            'created_at': created,
            'type': jtype,
        }
        if jtype == 'render':
            job.update(mode=mode, items=[{'no': 1}])
        status = {
            'job_id': job_id,
            'state': state,
            'started_at': started,
            'updated_at': updated,
            'items': [{'no': 1, 'state': state}] if jtype == 'render' else [],
            'message': '完成' if state == 'done' else '处理中',
        }
        atomic_json(self.jobs_dir / f'{job_id}.json', job)
        atomic_json(self.jobs_dir / f'{job_id}.status.json', status)
        return job, status

    def test_expired_terminal_jobs_archived_and_searchable_without_top_level_index(self):
        task = self.studio.create({'product_dir': str(self.product)})
        tid = task['id']
        # 1) 超期已结束作业（10 天前 done / failed）应归档
        self._write_job_pair('20261101-100000-aa01', tid, jtype='export_preview', state='done', days_ago=10)
        self._write_job_pair('20261101-100500-aa02', tid, jtype='render', mode='smallshot', state='done', days_ago=9)
        self._write_job_pair('20261101-101000-aa03', tid, jtype='render', mode='final', state='failed', days_ago=8)
        # 2) 未超期已结束作业（2 天前 done）与运行中作业（12 天前 running）保留在顶层
        self._write_job_pair('20261124-100000-bb01', tid, jtype='render', mode='smallshot', state='done', days_ago=2)
        self._write_job_pair('20261101-090000-cc01', tid, jtype='render', mode='final', state='running', days_ago=12)

        loaded = self.studio.load(tid)
        loaded['items'][0]['smallshot'].update(job_id='20261101-100500-aa02', state='queued')
        self.studio.save(loaded)

        res = self.cleanup.archive_jobs()
        self.assertEqual(res, {'archived_jobs': 3})

        # 顶层目录绝无 index.json 或已归档作业，只剩活跃作业
        top_jsons = sorted(p.name for p in self.jobs_dir.iterdir() if p.is_file() and p.suffix == '.json')
        self.assertEqual(top_jsons, [
            '20261101-090000-cc01.json',
            '20261101-090000-cc01.status.json',
            '20261124-100000-bb01.json',
            '20261124-100000-bb01.status.json',
        ])
        self.assertFalse((self.jobs_dir / 'index.json').exists())

        # 归档目录包含 3 对作业文件和 index.json
        archive_dir = self.jobs_dir / 'archive'
        self.assertTrue((archive_dir / 'index.json').is_file())
        for archived_id in ('20261101-100000-aa01', '20261101-100500-aa02', '20261101-101000-aa03'):
            self.assertTrue((archive_dir / f'{archived_id}.json').is_file())
            self.assertTrue((archive_dir / f'{archived_id}.status.json').is_file())

        # JobIndex.for_task 与 JobIndex.get（包括全新创建的 JobIndex 实例）均可无缝查到全部 5 个作业
        fresh_index = JobIndex(self.jobs_dir, read_json)
        all_for_task = fresh_index.for_task(tid)
        self.assertEqual([j['job_id'] for j, _ in all_for_task], [
            '20261101-090000-cc01',
            '20261101-100000-aa01',
            '20261101-100500-aa02',
            '20261101-101000-aa03',
            '20261124-100000-bb01',
        ])
        got_archived = fresh_index.get('20261101-100500-aa02')
        self.assertIsNotNone(got_archived)
        self.assertEqual(got_archived[1]['state'], 'done')
        got_active = fresh_index.get('20261124-100000-bb01')
        self.assertIsNotNone(got_active)
        self.assertEqual(got_active[1]['state'], 'done')
        self.assertIsNone(fresh_index.get('nonexistent-job'))

        # describe 合并归档作业后，小样状态与预览状态仍能还原为 done
        described = self.cleanup.describe(tid)
        self.assertEqual(described['preview']['state'], 'done')
        self.assertEqual(described['task']['items'][0]['smallshot']['state'], 'done')

        # 结束唯一运行中的 cc01 并按 jobs_archive_after_days=0 全部归档后，preview() 复用已归档预览且 delete_task() 不误报 409
        atomic_json(self.jobs_dir / '20261101-090000-cc01.status.json', {
            'job_id': '20261101-090000-cc01', 'state': 'done', 'updated_at': _iso_days_ago(1),
            'items': [{'no': 1, 'state': 'done'}],
        })
        self.studio.config['jobs_archive_after_days'] = 0
        self.cleanup.archive_jobs(before=time.time() + 10)
        self.assertEqual([p.name for p in self.jobs_dir.iterdir() if p.is_file()], [])
        preview_res = self.studio.preview(tid, {})
        self.assertEqual(preview_res['job']['job_id'], '20261101-100000-aa01')
        deleted = self.studio.delete_task(tid)
        self.assertEqual(deleted['deleted'], tid)


class AtomicDeliverNoPrevTests(unittest.TestCase):
    def test_deliver_stages_atomically_archives_old_and_creates_no_prev(self):
        root, product = _make_studio_root(self, 'deliver-')
        studio = Studio(root)
        self.addCleanup(studio.calibration.close)
        task = studio.create({'product_dir': str(product)})
        tid = task['id']
        # 开启自定义打光，连同 光向参考/ 一起验证无 .prev 且原子替换
        studio.update(tid, {
            'lighting': {
                'enabled': True,
                'azimuth_deg': -40,
                'elevation_deg': 35,
                'intensity': 1.0,
                'contrast': 0.5,
            }
        })
        studio.add_lighting_image(tid, 1, PNG_1X1)
        folder = studio.task_dir(tid)
        (folder / 'final').mkdir(parents=True, exist_ok=True)
        (folder / 'final' / '1.jpg').write_bytes(b'v1-image')
        (folder / 'final' / '1_遮罩_ID.jpg').write_bytes(b'v1-mask')
        loaded = studio.load(tid)
        loaded['items'][0]['final']['state'] = 'done'
        studio.save(loaded)

        # 第一次入库
        plan1 = studio.delivery_plan(tid)
        delivered1 = studio.deliver(tid, plan1['token'])
        sku = Path(delivered1['sku_dir'])
        self.assertIsNone(delivered1['archive_dir'])
        self.assertEqual((sku / '1.jpg').read_bytes(), b'v1-image')
        self.assertEqual((sku / '1_遮罩_ID.jpg').read_bytes(), b'v1-mask')
        self.assertTrue((sku / '白底清单.json').is_file())
        self.assertTrue((product / '光向参考' / '1.png').is_file())
        self.assertEqual(list(sku.rglob('*.prev')), [])
        self.assertEqual(list(sku.glob('.staging-*')), [])

        # 更新成品后第二次入库：验证写入 .staging-<uuid>、旧版归档、且全目录无任何 .prev
        (folder / 'final' / '1.jpg').write_bytes(b'v2-image')
        (folder / 'final' / '1_遮罩_ID.jpg').write_bytes(b'v2-mask')
        seen_staging = []
        real_copy2 = shutil.copy2

        def spy_copy2(src, dst, *args, **kwargs):
            seen_staging.append(str(dst))
            return real_copy2(src, dst, *args, **kwargs)

        plan2 = studio.delivery_plan(tid)
        with patch('server.core.shutil.copy2', side_effect=spy_copy2):
            delivered2 = studio.deliver(tid, plan2['token'])

        self.assertTrue(any('.staging-' in p for p in seen_staging))
        self.assertIsNotNone(delivered2['archive_dir'])
        archive_dir = Path(delivered2['archive_dir'])
        self.assertEqual((archive_dir / '1.jpg').read_bytes(), b'v1-image')
        self.assertEqual((archive_dir / '1_遮罩_ID.jpg').read_bytes(), b'v1-mask')
        self.assertTrue((archive_dir / '白底清单.json').is_file())
        self.assertEqual((sku / '1.jpg').read_bytes(), b'v2-image')
        self.assertEqual((sku / '1_遮罩_ID.jpg').read_bytes(), b'v2-mask')
        # sku/ 与 光向参考/ 均无任何 .prev 或残留 .staging-* 目录
        self.assertEqual(list(sku.rglob('*.prev')), [])
        self.assertEqual(list((product / '光向参考').rglob('*.prev')), [])
        self.assertEqual(list(sku.glob('.staging-*')), [])
        self.assertEqual(list((product / '光向参考').glob('.staging-*')), [])


class DeferredStartupTests(unittest.TestCase):
    def test_defer_startup_runs_heavy_tasks_in_background_after_http_ready(self):
        root, product = _make_studio_root(self, 'defer-')
        # 预先构造一条残留 thinking 状态的校准任务和一条超过 30 天的回收站目录
        seed = Studio(root)
        task = seed.create({'product_dir': str(product)})
        tid = task['id']
        seed.calibration.close()
        task_doc = read_json(seed.task_dir(tid) / 'task.json')
        task_doc['ai_calibration']['state'] = 'thinking'
        task_doc['ai_calibration']['rounds'] = [{
            'n': 1, 'job_id': '20261101-000000-0001', 'overrides': None,
            'verdict': None, 'summary': '', 'notes': [], 'confidence': None,
            'stats_file': None, 'error': None,
        }]
        atomic_json(seed.task_dir(tid) / 'task.json', task_doc)

        old_trash = root / 'exchange' / 'trash' / '20260101-000000-dead'
        old_trash.mkdir(parents=True)
        (old_trash / 'task.json').write_text('{}', encoding='utf-8')
        old_epoch = time.time() - 40 * DAY
        os.utime(old_trash, (old_epoch, old_epoch))

        # defer_startup=True：构造 Studio 时不阻塞跑 recover / purge_trash
        deferred = Studio(root, defer_startup=True)
        self.addCleanup(deferred.calibration.close)
        self.assertFalse(deferred.startup_done.is_set())
        self.assertTrue(old_trash.exists())
        self.assertEqual(read_json(deferred.task_dir(tid) / 'task.json')['ai_calibration']['state'], 'thinking')

        # 启动 LocalServer：HTTP 立即监听可用，后台线程自动完成初始化清理与恢复
        server = LocalServer(('127.0.0.1', 0), handler_for(deferred))
        srv_thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        srv_thread.start()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/health', timeout=2) as resp:
                self.assertEqual(resp.status, 200)
            self.assertTrue(deferred.startup_done.wait(timeout=5))
            self.assertFalse(old_trash.exists())
            self.assertEqual(read_json(deferred.task_dir(tid) / 'task.json')['ai_calibration']['state'], 'failed')
        finally:
            server.shutdown()
            server.server_close()
            srv_thread.join(timeout=3)

    def test_desktop_main_creates_window_before_slow_max_probe(self):
        temp_root, _ = _make_studio_root(self, 'desk-fast-')
        order = []
        probe_gate = threading.Event()

        class _FakeEvent:
            def __iadd__(self, _cb):
                return self

            def is_set(self):
                return True

        fake_window = Mock()
        fake_window.events.loaded = _FakeEvent()
        fake_window.events.shown = _FakeEvent()
        fake_window.events.closing = _FakeEvent()
        fake_webview = Mock()
        fake_webview.settings = {}

        def on_create_window(*args, **kwargs):
            order.append('create_window')
            probe_gate.set()
            return fake_window

        def slow_ensure_max(_root):
            probe_gate.wait(timeout=2)
            order.append('ensure_max_exe')
            return None

        fake_webview.create_window.side_effect = on_create_window
        with patch.dict(sys.modules, {'webview': fake_webview}), \
                patch.object(desktop, 'ROOT', temp_root), \
                patch.object(desktop, 'ensure_max_exe', side_effect=slow_ensure_max), \
                patch.object(desktop, 'refresh_max_plugin'), \
                patch.object(desktop, 'start_service', return_value=(None, None)), \
                patch.object(desktop, 'stop_service'), \
                patch.object(desktop.ctypes.windll.user32, 'MessageBoxW') as msgbox, \
                patch('server.node_setup.ensure_service'), \
                patch.object(sys, 'argv', ['desktop.pyw']):
            rc = desktop.main()
        msgbox.assert_not_called()
        self.assertEqual(rc, 0)
        self.assertEqual(order[0], 'create_window')
        self.assertIn('ensure_max_exe', order)


class MaterialsContractWiringTests(unittest.TestCase):
    def setUp(self):
        self.root, self.product = _make_studio_root(self, 'materials-')
        self.studio = Studio(self.root)
        self.addCleanup(self.studio.calibration.close)
        self.server = LocalServer(('127.0.0.1', 0), handler_for(self.studio))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def _req(self, method, path, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
        headers = {'Content-Type': 'application/json'} if data is not None else {}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read())

    def test_materials_route_describe_backfill_and_submit_effective_overrides(self):
        task = self.studio.create({'product_dir': str(self.product)})
        tid = task['id']

        # 模拟 calib-refs 的 server.materials 契约（Contract 2 & 3）
        fake_materials = types.ModuleType('server.materials')

        def effective_overrides(t):
            manual = (t.get('material_overrides') or {}).get('materials') or {}
            if not manual:
                return t.get('ai_calibration', {}).get('accepted_overrides')
            return {'materials': manual, 'lights': {}, 'environment': {'reflection_strength_mul': 1.2}, 'items': {}}

        def scene_materials_detail(studio_inst, t, jobs=None):
            return {
                'materials': [
                    {'name': '柜门 胡桃饰面', 'kind': 'wood'},
                    {'name': '长虹玻璃门', 'kind': 'glass'},
                ],
                'environment': {'reflection_strength': 1.0},
                'material_overrides': t.get('material_overrides') or {},
                'effective_overrides': effective_overrides(t),
                'material_bindings': t.get('material_bindings') or {},
            }

        def update_task_materials(studio_inst, task_id, data):
            # 验证调用时外部未死锁且请求体已在锁外解析为 dict
            assert isinstance(data, dict)
            with studio_inst.lock:
                cur = studio_inst.load(task_id)
                cur['material_overrides'] = data.get('material_overrides', {})
                if 'material_bindings' in data:
                    cur['material_bindings'] = data['material_bindings']
                studio_inst.save(cur)
                return {
                    'task': cur,
                    'materials_detail': scene_materials_detail(
                        studio_inst, cur, studio_inst.job_index.for_task(task_id)
                    ),
                }

        fake_materials.effective_overrides = effective_overrides
        fake_materials.scene_materials_detail = scene_materials_detail
        fake_materials.update_task_materials = update_task_materials

        with patch.object(core_mod, '_materials', fake_materials), \
                patch.object(server_app, '_materials', fake_materials):
            # 1) GET /api/tasks/<id> 返回 materials_detail，且当 scene_materials 为空时由 detail['materials'] 回填名称列表
            status_get, described = self._req('GET', f'/api/tasks/{tid}')
            self.assertEqual(status_get, 200)
            self.assertIn('materials_detail', described)
            self.assertEqual(described['scene_materials'], ['柜门 胡桃饰面', '长虹玻璃门'])
            sig_before = described['signature']

            # 2) PUT /api/tasks/<id>/materials 保存手动材质覆盖并改变 signature
            payload = {
                'material_overrides': {
                    'materials': {'柜门 胡桃饰面': {'brightness_mul': 1.35, 'glossiness_delta': -0.05}}
                },
                'material_bindings': {'柜门 胡桃饰面': '胡桃木'},
            }
            status_put, put_res = self._req('PUT', f'/api/tasks/{tid}/materials', payload)
            self.assertEqual(status_put, 200)
            self.assertEqual(
                put_res['materials_detail']['material_overrides']['materials']['柜门 胡桃饰面']['brightness_mul'],
                1.35,
            )
            _, described_after = self._req('GET', f'/api/tasks/{tid}')
            self.assertNotEqual(sig_before, described_after['signature'])

            # 3) 出小样与开始渲染均使用 _materials.effective_overrides(task)
            self.studio.config['calibration_auto'] = False
            status_job, job_res = self._req('POST', f'/api/tasks/{tid}/jobs', {'mode': 'smallshot'})
            self.assertEqual(status_job, 201)
            self.assertEqual(
                job_res['job']['overrides']['materials']['柜门 胡桃饰面']['brightness_mul'],
                1.35,
            )
            self.assertEqual(job_res['job']['overrides']['environment']['reflection_strength_mul'], 1.2)

            status_final, final_res = self._req('POST', f'/api/tasks/{tid}/jobs', {'mode': 'final'})
            self.assertEqual(status_final, 201)
            self.assertEqual(
                final_res['job']['overrides']['materials']['柜门 胡桃饰面']['brightness_mul'],
                1.35,
            )

    def test_standalone_fallback_when_materials_module_absent(self):
        task = self.studio.create({'product_dir': str(self.product)})
        tid = task['id']
        preview_dir = self.studio.task_dir(tid) / 'preview'
        preview_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(preview_dir / 'scene_manifest.json', {
            'materials': [{'name': '北欧橡木柜体', 'kind': 'wood'}],
            'environment': {'reflection_strength': 1.0},
        })
        with patch.object(core_mod, '_materials', None), \
                patch.object(server_app, '_materials', None), \
                patch.dict(sys.modules, {'server.materials': None}):
            _, desc = self._req('GET', f'/api/tasks/{tid}')
            self.assertEqual(desc['scene_materials'], ['北欧橡木柜体'])
            status_put, _ = self._req('PUT', f'/api/tasks/{tid}/materials', {
                'materials': {'北欧橡木柜体': {'brightness_mul': 1.15}},
            })
            self.assertEqual(status_put, 200)
            self.studio.config['calibration_auto'] = False
            status_job, job_res = self._req('POST', f'/api/tasks/{tid}/jobs', {'mode': 'smallshot'})
            self.assertEqual(status_job, 201)
            self.assertEqual(job_res['job']['overrides']['materials']['北欧橡木柜体']['brightness_mul'], 1.15)

    def test_real_materials_module_end_to_end(self):
        task = self.studio.create({'product_dir': str(self.product)})
        tid = task['id']
        preview_dir = self.studio.task_dir(tid) / 'preview'
        preview_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(preview_dir / 'scene_manifest.json', {
            'materials': [
                {'name': '柜门_胡桃01', 'kind': 'wood', 'class': 'CoronaPhysicalMtl', 'objects': 3, 'color': [120, 82, 54]},
                {'name': '长虹玻璃门', 'kind': 'glass', 'class': 'CoronaPhysicalMtl', 'objects': 1, 'color': [235, 240, 242]},
            ],
            'environment': {'reflect_map': 'studio_hdr.exr'},
        })
        self.studio.refs.save({
            'materials': {
                '胡桃木': {'keywords': ['胡桃木', '胡桃'], 'images': []},
                '长虹玻璃': {'keywords': ['长虹玻璃', '长虹'], 'images': []},
            },
            'style': [],
            'annotated': [],
        })
        _, desc = self._req('GET', f'/api/tasks/{tid}')
        self.assertEqual(desc['scene_materials'], ['柜门_胡桃01', '长虹玻璃门'])
        mats_by_name = {m['name']: m for m in desc['materials_detail']['materials']}
        self.assertEqual(mats_by_name['柜门_胡桃01']['effective_ref'], '胡桃木')
        self.assertEqual(mats_by_name['长虹玻璃门']['effective_ref'], '长虹玻璃')

        status_put, put_res = self._req('PUT', f'/api/tasks/{tid}/materials', {
            'action': 'set',
            'material_overrides': {
                'materials': {
                    '柜门_胡桃01': {'brightness_mul': 1.4, 'reflection_mul': 0.2},
                    '长虹玻璃门': {'refraction_mul': 1.15, 'ior': 1.52},
                },
                'environment': {'reflection_strength_mul': 0.0},
            },
            'material_bindings': {'长虹玻璃门': '__none__'},
        })
        self.assertEqual(status_put, 200)
        self.assertEqual(put_res['materials_detail']['effective_overrides']['environment']['reflection_strength_mul'], 0.0)
        self.assertIsNone(
            {m['name']: m for m in put_res['materials_detail']['materials']}['长虹玻璃门']['effective_ref']
        )

        self.studio.config['calibration_auto'] = False
        status_job, job_res = self._req('POST', f'/api/tasks/{tid}/jobs', {'mode': 'smallshot'})
        self.assertEqual(status_job, 201)
        self.assertEqual(job_res['job']['overrides']['materials']['柜门_胡桃01']['brightness_mul'], 1.4)
        self.assertEqual(job_res['job']['overrides']['materials']['长虹玻璃门']['ior'], 1.52)
        self.assertEqual(job_res['job']['overrides']['environment']['reflection_strength_mul'], 0.0)


if __name__ == '__main__':
    unittest.main()
