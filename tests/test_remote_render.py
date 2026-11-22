"""内网渲染机：在本机起一个渲染机服务模拟另一台电脑，走通配对、传模型、预览、小样、AI 校准轮、离线和换回本机。
渲染机那边用假执行器（tools/fake_runner）代替 Max。"""
import json
import socket
import sys
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from max import wb_core
from server.core import Studio, read_json, atomic_json
from server.remote_client import RemoteError, discover, parse_address
from server.remote_protocol import ProtocolError, extract_bundle, node_job, safe_rel
from server.render_node import NodeState, serve
from tools.fake_runner import run_once
from test_temp_dirs import test_directory


class RemoteRenderTests(unittest.TestCase):
    def setUp(self):
        self.config = read_json(ROOT / 'config.json')
        self.client_root = test_directory(self, 'remote-client-')
        self.node_root = test_directory(self, 'remote-node-')
        (self.node_root / 'template').mkdir()
        (self.node_root / 'template' / '白底模板.max').write_bytes(b'template')
        self.fixture = self.node_root / 'fixture.fbx'
        self.fixture.write_bytes(b'FBX placeholder')
        self.service = serve(self.node_root, host='127.0.0.1', port=0, discovery_port=0, config=self.config)
        self.addCleanup(self.service.close)
        self.node_exchange = self.service.node.exchange
        self.studio = Studio(self.client_root, self.config)
        self.addCleanup(self.studio.calibration.close)
        self.remote = self.studio.remote
        product = self.client_root / '产品'
        (product / '模型' / 'maps').mkdir(parents=True)
        (product / '模型' / '柜体.max').write_bytes(b'max file')
        (product / '模型' / 'maps' / '木纹.jpg').write_bytes(b'texture')
        (product / '模型' / '旧模型.zip').write_bytes(b'archive')
        (product / '参考图').mkdir()
        (product / '参考图' / '参考.jpg').write_bytes(b'photo')
        (product / 'sku').mkdir()
        (product / 'sku' / '1.jpg').write_bytes(b'delivered')
        self.task = self.studio.create({'product_dir': str(product)})

    # ── 辅助 ──
    def code(self):
        return NodeState(self.node_root).read()['code']

    def connect(self):
        node = self.remote.pair('127.0.0.1', self.service.port, self.code())
        self.remote.select(node)
        return node

    def node_jobs(self):
        return sorted(p for p in (self.node_exchange / 'jobs').glob('*.json') if not p.name.endswith('.status.json'))

    def status(self, job_id):
        return read_json(self.studio.exchange / 'jobs' / (job_id + '.status.json'))

    def beat(self):
        atomic_json(self.node_exchange / 'runner.heartbeat.json',
                    {'epoch': time.time(), 'busy': False, 'job_id': None, 'runner_version': 'fake'})

    # ── 测试 ──
    def test_pairing_requires_code_and_limits_attempts(self):
        with self.assertRaises(RemoteError) as wrong:
            self.remote.pair('127.0.0.1', self.service.port, '000000' if self.code() != '000000' else '111111')
        self.assertEqual(wrong.exception.status, 403)
        with self.assertRaises(RemoteError):
            self.remote.select({'host': '127.0.0.1', 'port': self.service.port})   # 没配对不能选
        for _ in range(4):
            with self.assertRaises(RemoteError):
                self.remote.pair('127.0.0.1', self.service.port, '999998' if self.code() != '999998' else '999997')
        with self.assertRaises(RemoteError) as blocked:
            self.remote.pair('127.0.0.1', self.service.port, self.code())
        self.assertEqual(blocked.exception.status, 429)

    def test_preview_smallshot_and_calibration_round_through_node(self):
        self.connect()
        task_id = self.task['id']
        preview = self.studio.preview(task_id, {})['job']
        # 本机执行器不会接已经交给渲染机的作业
        self.assertEqual(wb_core.pending_jobs(self.studio.exchange / 'jobs'), [])
        self.assertEqual(self.status(preview['job_id'])['remote']['phase'], 'pending')
        self.remote.tick()
        self.assertEqual(self.status(preview['job_id'])['remote']['phase'], 'sent')
        [node_file] = self.node_jobs()
        job = read_json(node_file)
        workspace = self.node_exchange / 'tasks' / job['task_id']
        self.assertTrue(job['task_id'].startswith('remote-'))
        self.assertEqual(Path(job['template_max']), (self.node_root / 'template' / '白底模板.max').resolve())
        self.assertTrue(Path(job['max_file']).is_file())
        self.assertEqual(Path(job['out_fbx']), workspace / 'preview' / 'preview.fbx')
        bundle = Path(job['asset_dirs'][0])
        names = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file())
        self.assertEqual(names, ['模型/maps/木纹.jpg', '模型/柜体.max'])   # 参考图、sku、压缩包不传
        # 渲染机上的 Max 还没打开：说清楚在等什么
        self.remote.tick()
        self.assertIn('Max 没打开', self.status(preview['job_id'])['message'])
        run_once(self.node_exchange, self.fixture)
        self.remote.tick()
        self.assertEqual(self.status(preview['job_id'])['state'], 'done')
        task_dir = self.studio.task_dir(task_id)
        self.assertEqual((task_dir / 'preview' / 'preview.fbx').read_bytes(), b'FBX placeholder')
        self.assertTrue((task_dir / 'preview' / 'bbox.json').is_file())
        self.assertEqual(self.studio.describe(task_id)['preview']['state'], 'done')

        render = self.studio.submit(task_id, {'mode': 'smallshot'})
        self.remote.tick()
        self.assertEqual(len(self.node_jobs()), 2)
        run_once(self.node_exchange, self.fixture)
        self.remote.tick()
        status = self.status(render['job_id'])
        self.assertEqual(status['state'], 'done')
        self.assertIn('渲染机', status['message'])
        for item in status['items']:
            self.assertTrue(Path(item['out_image']).is_file())
            self.assertTrue(Path(item['out_image']).resolve().is_relative_to(task_dir.resolve()))
        items = self.studio.describe(task_id)['task']['items']
        self.assertTrue(all(i['smallshot']['state'] == 'done' for i in items if i['enabled']))
        # 同一个产品第二次出图不再传模型
        self.assertEqual(len(list((self.node_exchange / 'remote-cache' / 'bundles').rglob('柜体.max'))), 1)

        # AI 校准轮：执行器写到任务目录下 calibration/round-N 的文件也要拉回来
        task = self.studio.load(task_id)
        calibration = self.studio._submit_render(task, 'smallshot', round_no=0)
        self.remote.tick()
        run_once(self.node_exchange, self.fixture)
        self.remote.tick()
        self.assertEqual(self.status(calibration['job_id'])['state'], 'done')
        round_dir = task_dir / 'calibration' / 'round-0'
        self.assertTrue((round_dir / 'scene_manifest.json').is_file())
        self.assertTrue((round_dir / 'regions' / '1.png').is_file())

    def test_runner_status_follows_selected_node(self):
        self.assertIsNone(self.studio.runner_status()['target'])
        self.connect()
        self.remote.tick()
        status = self.studio.runner_status()
        self.assertEqual(status['target']['name'], socket.gethostname())
        self.assertTrue(status['target']['paired'])
        self.assertFalse(status['online'])            # 渲染机上执行器没心跳
        self.assertFalse(status['can_start'])
        self.beat()
        self.remote.infos.clear()
        self.remote.tick()
        self.assertTrue(self.studio.runner_status()['online'])

    def test_offline_node_keeps_job_and_switching_back_releases_it(self):
        self.connect()
        self.service.close()
        job = self.studio.submit(self.task['id'], {'mode': 'smallshot'})
        self.remote.tick()
        status = self.status(job['job_id'])
        self.assertEqual(status['remote']['phase'], 'pending')
        self.assertIn('连不上', status['message'])
        self.remote.select(None)
        self.assertFalse((self.studio.exchange / 'jobs' / (job['job_id'] + '.status.json')).exists())
        self.assertEqual([j.data['job_id'] for j in wb_core.pending_jobs(self.studio.exchange / 'jobs')],
                         [job['job_id']])

    def test_discovery_answers_with_node_info(self):
        found = discover(timeout=1, port=self.service.discovery.port, targets=['127.0.0.1'])
        [(host, port)] = found.keys()
        self.assertEqual(port, self.service.port)
        self.assertEqual(found[(host, port)]['name'], socket.gethostname())

    def test_choices_lists_known_node(self):
        node = self.connect()
        choices = self.remote.choices(scan=False)
        [entry] = choices['nodes']
        self.assertTrue(entry['paired'] and entry['selected'] and entry['reachable'])
        self.assertEqual(choices['target']['port'], node['port'])

    def test_node_rejects_paths_outside_task_and_bad_bundles(self):
        bundle = self.node_root / 'bundle'
        (bundle / '模型').mkdir(parents=True)
        (bundle / '模型' / 'a.max').write_bytes(b'x')
        task_id = '20261122-101010-abcd'
        base = {'type': 'export_preview', 'job_id': '20261122-101011-abcd', 'task_id': task_id,
                'created_at': '2026-11-22T10:10:11+08:00', 'max_file': '@model:模型/a.max'}
        kwargs = dict(node_exchange=self.node_exchange, node_job_id='x', node_task_id='remote-1-' + task_id,
                      bundle_dir=bundle, template=self.node_root / 'template' / '白底模板.max')
        job, _ = node_job({**base, 'out_fbx': f'@ex:tasks/{task_id}/preview/preview.fbx'}, **kwargs)
        self.assertTrue(job['out_fbx'].endswith('preview.fbx'))
        for bad in ('@ex:tasks/20261122-000000-0000/preview/x.fbx', '@ex:tasks/../../x.fbx', r'C:\Windows\x.fbx'):
            with self.subTest(bad=bad), self.assertRaises(ProtocolError):
                node_job({**base, 'out_fbx': bad}, **kwargs)
        with self.assertRaises(ProtocolError):
            node_job({**base, 'max_file': '@model:../a.max'}, **kwargs)
        archive = self.node_root / 'evil.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('../escape.txt', 'x')
        with self.assertRaises(ProtocolError):
            extract_bundle(archive, self.node_root / 'out')
        self.assertFalse((self.node_root / 'escape.txt').exists())
        for value in ('a/../b', '/abs', 'C:/x', 'a\\b', ''):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                safe_rel(value)

    def test_unpaired_requests_are_refused(self):
        from server.remote_client import NodeClient
        client = NodeClient('127.0.0.1', self.service.port, token='wrong')
        info = client.info()
        self.assertFalse(info['paired'])
        with self.assertRaises(RemoteError) as error:
            client.job('20261122-101011-abcd')
        self.assertEqual(error.exception.status, 401)

    def test_parse_address(self):
        self.assertEqual(parse_address('render-pc'), ('render-pc', 8775))
        self.assertEqual(parse_address(' 192.168.1.20:9000 '), ('192.168.1.20', 9000))
        for bad in ('', 'a b', 'x:abc', 'x:0', '\\\\pc\\share'):
            with self.subTest(bad=bad), self.assertRaises(RemoteError):
                parse_address(bad)


class RunnerSideTests(unittest.TestCase):
    def test_lock_script_falls_back_to_bundled_copy(self):
        bundled = ROOT / 'max' / 'pf_max_lock.ms'
        self.assertTrue(bundled.is_file())
        self.assertEqual(wb_core.lock_script_path(ROOT / 'nope.ms', bundled), bundled)
        self.assertEqual(wb_core.lock_script_path(bundled, ROOT / 'nope.ms'), bundled)

    def test_asset_search_dirs_includes_subfolders(self):
        root = test_directory(self, 'asset-dirs-')
        (root / 'a' / 'maps').mkdir(parents=True)
        dirs = wb_core.asset_search_dirs({'asset_dirs': [str(root / 'a'), 'relative', str(root / 'missing')]})
        self.assertEqual(dirs, [str(root / 'a'), str(root / 'a' / 'maps')])


if __name__ == '__main__':
    unittest.main()
