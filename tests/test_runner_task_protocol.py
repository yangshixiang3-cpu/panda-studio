"""执行器接口、轮询缓存和手动校准的回归测试。"""
import copy
from datetime import datetime, timezone
import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.app import LocalServer, handler_for
from server.core import Studio, StudioError, atomic_json, read_json
from tools.fake_runner import run_once
from test_temp_dirs import test_directory
from fixture_files import needs_product_fbx


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


@needs_product_fbx
class TaskProtocolTests(unittest.TestCase):
    def setUp(self):
        self.root = test_directory(self, 'protocol-')
        config = read_json(ROOT/'config.json')
        config.update(max_exe='3dsmax.exe', smallshot_size={'portrait': [60, 80], 'landscape': [80, 60]},
                      calibration_max_rounds=0, calibration_timeout_sec=10,
                      codex_command=[sys.executable, str(ROOT/'tests/fake_codex.py')])
        self.studio = Studio(self.root, config)
        self.addCleanup(self.studio.calibration.close)
        product = self.root/'产品'
        product.mkdir()
        (product/'产品.max').touch()
        self.task = self.studio.create({'product_dir': str(product)})
        self.id = self.task['id']
        self.jobs = self.studio.exchange/'jobs'
        self.heartbeat = self.studio.exchange/'runner.heartbeat.json'
        self.exe = self.root/'3dsmax.exe'
        self.probe = self.enterContext(patch.object(self.studio.runner, '_probe_max', return_value=False))

    def write_heartbeat(self, seconds, **fields):
        atomic_json(self.heartbeat, {'ts': timestamp(seconds), 'epoch': seconds, 'pid': 1,
                                    'busy': True, 'job_id': 'job', 'runner_version': 'fake', **fields})

    def status(self, job, **fields):
        atomic_json(self.jobs/(job['job_id']+'.status.json'), fields)

    def fake_render(self):
        run_once(self.studio.exchange, ROOT/'tests/fixtures/L型咖啡柜_绑定.fbx')

    def add_views(self):
        main = self.task['items'][0]
        items = [main]
        for no, (label, view) in enumerate((('正面', 'front'), ('侧面', 'side'), ('俯视', 'top')), 2):
            items.append({**main, 'no':no, 'label':label, 'view':view})
        self.studio.update(self.id, {'items':items})

    def test_heartbeat_age_boundary_missing_and_stopped(self):
        self.exe.touch()
        with patch('server.runner.time.time', return_value=100):
            self.assertEqual(self.studio.runner.status(),
                             dict(online=False, age_sec=None, busy=False, job_id=None,
                                  max_running=False, max_windows=[], max_hidden=0, can_start=True))
            self.write_heartbeat(88)
            fresh = self.studio.runner.status()
            self.assertEqual((fresh['online'], fresh['age_sec'], fresh['busy'], fresh['job_id']),
                             (True, 12, True, 'job'))
            self.assertFalse(fresh['can_start'])
            self.write_heartbeat(87)
            self.assertFalse(self.studio.runner.status()['online'])
            self.write_heartbeat(100, stopped=True)
            self.assertFalse(self.studio.runner.status()['online'])
        self.probe.assert_called_once()

    def test_busy_heartbeat_stays_online_while_max_renders(self):
        self.probe.return_value = True
        with patch('server.runner.time.time', return_value=2000):
            self.write_heartbeat(1400)
            self.assertTrue(self.studio.runner.status()['online'])
            self.write_heartbeat(199)
            self.assertFalse(self.studio.runner.status()['online'])
            self.write_heartbeat(1400, busy=False)
            self.assertFalse(self.studio.runner.status()['online'])
            self.write_heartbeat(1400, stopped=True)
            self.assertFalse(self.studio.runner.status()['online'])

    def test_start_http_branches_and_origin(self):
        server = LocalServer(('127.0.0.1', 0), handler_for(self.studio))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(origin=None):
            url = f'http://127.0.0.1:{server.server_port}/api/runner/start'
            headers = {'Origin': origin} if origin else {}
            try:
                with urlopen(Request(url, method='POST', headers=headers), timeout=5) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                with error:
                    return error.code, json.load(error)

        with patch('server.runner.subprocess.Popen') as start:
            self.assertEqual(request('https://example.test'), (403, {'error': '禁止来自外部网页的写入。'}))
            self.write_heartbeat(time.time())
            self.assertEqual(request(), (409, {'error': '执行器已经在运行。'}))
            self.write_heartbeat(time.time(), stopped=True)
            self.probe.return_value = True
            self.studio.runner._probe_until = 0
            self.assertEqual(request(), (409, {'error': '已经开着一个 Max（3ds Max），但它没有加载白底执行器。'
                                               '把这个 Max 关掉后再点「启动 Max」；如果关掉重开还是连不上，到「本机设置」点「一键查找并安装插件」。'}))
            self.probe.return_value = False
            self.studio.runner._probe_until = 0
            self.assertEqual(request(), (400, {'error': '找不到 3ds Max：到「本机设置」点「一键查找并安装插件」。'}))
            start.assert_not_called()
            self.exe.touch()
            self.assertEqual(request(), (200, {'started': True}))
            start.assert_called_once()
            script = str(self.root/'max/启动白底执行器.ms')
            self.assertEqual(start.call_args.args[0], [str(self.exe), '-U', 'MAXScript', script])
            self.assertEqual(start.call_args.kwargs['env']['WHITEBG_RUNNER_SCRIPT'], script)
            self.assertEqual(start.call_args.kwargs['stdout'], subprocess.DEVNULL)
            start.return_value.wait.assert_not_called()

    def test_progress_eta_signature_and_unchanged_task_file(self):
        self.add_views()
        self.assertEqual(self.studio.describe(self.id)['progress'],
                         dict(kind=None, stage=None, done=0, total=0, eta_sec=None))
        job = self.studio.submit(self.id, {'mode': 'smallshot'})
        queued = self.studio.describe(self.id)
        self.assertEqual(queued['progress'], dict(kind='smallshot', stage='queued', done=0, total=4, eta_sec=24))
        self.status(job, state='running', started_at=timestamp(90), updated_at=timestamp(98),
                    items=[{'no': 1, 'state': 'done'}, {'no': 2, 'state': 'rendering', 'started_at': timestamp(98)}])
        with patch('server.jobs.time.time', return_value=100):
            self.write_heartbeat(100)
            first = self.studio.describe(self.id)
        self.assertEqual(first['progress'], dict(kind='smallshot', stage='rendering', done=1, total=4, eta_sec=16))
        self.assertNotEqual(first['signature'], queued['signature'])
        task_file = self.studio.task_dir(self.id)/'task.json'
        original_mtime = task_file.stat().st_mtime_ns
        with patch('server.jobs.time.time', return_value=109):
            second = self.studio.describe(self.id)
        self.assertNotEqual(first['runner']['age_sec'], second['runner']['age_sec'])
        self.assertEqual(second['progress']['eta_sec'], 12)
        self.assertEqual(first['signature'], second['signature'])
        unchanged = self.studio.load(self.id)
        self.studio.update(self.id, {'items': unchanged['items']})
        self.assertEqual(task_file.stat().st_mtime_ns, original_mtime)
        changed = copy.deepcopy(unchanged['items'])
        changed[0]['parts'] = {'抽屉': .25}
        self.studio.update(self.id, {'items': changed})
        self.assertNotEqual(second['signature'], self.studio.describe(self.id)['signature'])
        self.assertEqual(list(task_file.parent.glob('task.json*.bak')), [])

    def test_job_index_loads_only_changed_current_status_and_updates_median(self):
        self.add_views()
        job = self.studio.submit(self.id, {'mode': 'smallshot'})
        for name, duration, fake in [('one', 40, False), ('two', 80, False), ('fake', 4, True)]:
            atomic_json(self.jobs/(name+'.json'), {**job, 'task_id': 'other', 'job_id': name})
            self.status({'job_id': name}, state='done', started_at=timestamp(50), updated_at=timestamp(50 + duration),
                        log=['假执行器处理完成。'] if fake else [])
        restarted = Studio(self.root, self.studio.config)
        self.addCleanup(restarted.calibration.close)
        self.assertEqual(restarted.job_index.seconds_per('smallshot'), 15)
        self.assertEqual(restarted.job_index.seconds_per('preview'), 8)
        self.assertEqual(restarted.job_index.seconds_per('final'), 100)
        self.status({'job_id': 'one'}, state='done', started_at=timestamp(50), updated_at=timestamp(170))
        self.status(job, state='done', started_at=timestamp(100), updated_at=timestamp(116))
        index = restarted.job_index
        with patch.object(index, 'read_json', wraps=read_json) as read:
            restarted.describe(self.id)
            restarted.describe(self.id)
        self.assertEqual([call.args[0].name for call in read.call_args_list], [job['job_id']+'.status.json'])
        self.assertEqual(index.seconds_per('smallshot'), 10)
        index.refresh_history()
        self.assertEqual(index.seconds_per('smallshot'), 20)

    def test_preview_final_and_thinking_eta(self):
        self.add_views()
        preview = self.studio.preview(self.id, {'source': 'export'})['job']
        self.status(preview, state='running', started_at=timestamp(96))
        with patch('server.jobs.time.time', return_value=100):
            self.assertEqual(self.studio.describe(self.id)['progress'],
                             dict(kind='preview', stage='rendering', done=0, total=1, eta_sec=4))
        self.status(preview, state='done', started_at=timestamp(96), updated_at=timestamp(105))
        final = self.studio.submit(self.id, {'mode': 'final'})
        self.assertEqual(self.studio.describe(self.id)['progress'],
                         dict(kind='final', stage='queued', done=0, total=4, eta_sec=400))
        self.status(final, state='done', started_at=timestamp(110), updated_at=timestamp(310))
        self.studio.describe(self.id)
        self.assertEqual(self.studio.job_index.seconds_per('preview'), 9)
        self.assertEqual(self.studio.job_index.seconds_per('final'), 50)

        task = self.studio.load(self.id)
        task['ai_calibration'].update(state='thinking', round=4, first_round=3, max_rounds=5,
                                      rounds=[{'n': 4, 'thinking_started_at': timestamp(90)}])
        self.studio.save(task)
        with patch('server.jobs.time.time', return_value=100):
            self.assertEqual(self.studio.describe(self.id)['progress'],
                             dict(kind='calibration', stage='thinking', done=1, total=3, eta_sec=30))

    def test_manual_start_requires_manifest_and_preserves_registered_note(self):
        job = self.studio.submit(self.id, {'mode': 'smallshot'})
        self.assertTrue(self.studio.load(self.id)['ai_calibration']['enabled'])
        self.assertFalse(job['want_manifest'])
        self.assertEqual(self.studio.load(self.id)['ai_calibration']['state'], 'idle')
        self.fake_render()
        with patch.object(self.studio.calibration, 'launch'):
            task = self.studio.calibration.action(self.id, {'action': 'start', 'user_note': '保留木纹'})
        cal = task['ai_calibration']
        self.assertEqual((cal['state'], cal['round'], cal['user_note']), ('rendering', 0, '保留木纹'))
        retry_job = self.studio.jobs_for(self.id)[-1]
        self.assertTrue(retry_job['want_manifest'])
        self.assertNotEqual(cal['rounds'][0]['job_id'], job['job_id'])
        self.assertEqual(self.studio.describe(self.id)['progress'],
                         dict(kind='calibration', stage='queued', done=0, total=1, eta_sec=26))

    def test_manual_start_reuses_complete_manifest_and_runs_fake_analysis(self):
        job = self.studio._submit_render(self.studio.load(self.id), 'smallshot', round_no=9)
        self.fake_render()
        task = self.studio.calibration.action(self.id, {'action': 'start', 'user_note': '沿用小样'})
        record = task['ai_calibration']['rounds'][0]
        self.assertEqual(task['ai_calibration']['state'], 'thinking')
        self.assertEqual((record['n'], record['job_id']), (0, job['job_id']))
        self.studio.calibration.thread.join(timeout=10)
        final = self.studio.load(self.id)['ai_calibration']
        self.assertEqual(final['state'], 'waiting_user', final['rounds'][0]['error'])
        self.assertEqual(len(self.studio.jobs_for(self.id)), 1)
        payload = read_json(self.studio.task_dir(self.id)/'calibration/round-0/input.json')
        self.assertEqual(payload['user_note'], '沿用小样')

    def test_manual_start_refuses_manifest_from_before_parts_change(self):
        job = self.studio._submit_render(self.studio.load(self.id), 'smallshot', round_no=9)
        self.fake_render()
        task = self.studio.merge(self.id)['task']
        task['items'][0]['parts'] = {'门': .5}
        self.studio.update(self.id, {'items': task['items']})
        with patch.object(self.studio.calibration, 'launch'):
            started = self.studio.calibration.action(self.id, {'action': 'start'})
        self.assertEqual(started['ai_calibration']['state'], 'rendering')
        self.assertNotEqual(started['ai_calibration']['rounds'][0]['job_id'], job['job_id'])

    def test_calibration_allows_appearance_and_blocks_structure_light_and_render(self):
        self.add_views()
        with patch.object(self.studio.calibration, 'launch'):
            self.studio.calibration.action(self.id, {'action': 'start'})
        items = self.studio.load(self.id)['items']
        items[0]['camera']['position_mm'][0] += 2
        items[0]['orientation'] = 'landscape' if items[0]['orientation'] == 'portrait' else 'portrait'
        items[0]['parts'] = {'门': .75}
        saved = self.studio.update(self.id, {'items': items})
        for k in ('orientation', 'parts'):
            self.assertEqual(saved['items'][0][k], items[0][k])
        self.assertEqual(saved['items'][0]['camera']['position_mm'], items[0]['camera']['position_mm'])
        disabled = copy.deepcopy(items)
        disabled[0]['enabled'] = False
        reordered = list(reversed(copy.deepcopy(items)))
        for no, item in enumerate(reordered, 1):
            item['no'] = no
        lit = copy.deepcopy(items)
        lit[0]['lighting'] = {'azimuth_deg': 30}
        for data in ({'items': disabled}, {'items': reordered}, {'items': items[:-1]}, {'items': lit},
                     {'lighting': {'azimuth_deg': 45}}):
            with self.subTest(data=data), self.assertRaises(StudioError) as error:
                self.studio.update(self.id, data)
            self.assertEqual((error.exception.status, str(error.exception)),
                             (409, 'AI 校准进行中：结束后再改打光或清单。'))
        for mode in ('smallshot', 'final'):
            with self.assertRaises(StudioError) as error:
                self.studio.submit(self.id, {'mode': mode})
            self.assertEqual((error.exception.status, str(error.exception)),
                             (409, 'AI 校准进行中，结束后再出图。'))
