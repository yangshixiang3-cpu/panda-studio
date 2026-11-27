"""1.7.2 安全加固与稳定性回归测试：输出白名单、切回本机竞态、渲染机限流/超时/并发、SSRF/重定向/密钥隔离、C 盘零写入、校准超时。"""
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from max import wb_core
from server import image_gen, node_setup, remote_protocol, render_node
from server.ai_settings import AIError, AISettings, dpapi, migrate_legacy_secrets, normalize_url
from server.calibration import WAIT_TIMEOUT_MSG
from server.core import Studio, atomic_json, read_json
from server.image_gen import ImageAPISettings, ImageGenerator, _download, _fetcher, _post_json
from server.remote_client import NodeClient, RemoteError
from server.remote_protocol import ProtocolError, extract_bundle, local_path, node_job, safe_output_rel
from server.render_node import HANDLER_TIMEOUT_SEC, NodeServer, NodeState, RenderNode, node_handler, serve
from server.runner import BUSY_GRACE_SEC, RENDERING_GRACE_SEC, RunnerStatus
from test_temp_dirs import test_directory


def _png_bytes(color='green'):
    buf = io.BytesIO()
    Image.new('RGB', (32, 32), color).save(buf, 'PNG')
    return buf.getvalue()


class OutputWhitelistAndPathTests(unittest.TestCase):
    def test_output_whitelist_allows_runner_outputs_and_blocks_task_root(self):
        good = (
            'preview/preview.fbx',
            'preview/bbox.json',
            'smallshots/1.jpg',
            'smallshots/_extra/1_遮罩_ID.jpg',
            'final/1.jpg',
            'final/1_遮罩_ID.jpg',
            'calibration/round-0/scene_manifest.json',
            'calibration/round-12/regions/legend.json',
            'calibration/round-3/regions/1.png',
            'calibration/round-0/smallshots/1.jpg',
            'preview/note.txt',
            'preview/run.log',
            'preview/thumb.webp',
            'smallshots/2.jpeg',
        )
        root = test_directory(self, 'wl-')
        tid = '20261126-101010-abcd'
        for rel in good:
            with self.subTest(good=rel):
                self.assertEqual(safe_output_rel(rel), rel)
                resolved = local_path(f'@ex:tasks/{tid}/{rel}', root, tid)
                self.assertTrue(Path(resolved).is_relative_to((root / 'tasks' / tid).resolve()))

        bad = (
            'task.json',
            '1.jpg',
            'references/1.jpg',
            'lighting/1.png',
            'calibration_refs/a.jpg',
            'calibration/scene_manifest.json',
            'calibration/round-bad/1.jpg',
            'calibration/round-0/evil.py',
            'preview/payload.exe',
            'final/script.bat',
            'smallshots/1.jpg.tmp',
            '../other/preview/1.jpg',
        )
        for rel in bad:
            with self.subTest(bad=rel):
                with self.assertRaises(ProtocolError):
                    safe_output_rel(rel)
                with self.assertRaises(ProtocolError):
                    local_path(f'@ex:tasks/{tid}/{rel}', root, tid)

    def test_rogue_node_cannot_overwrite_task_json(self):
        root = test_directory(self, 'rogue-node-')
        config = read_json(ROOT / 'config.json')
        studio = Studio(root, config)
        self.addCleanup(studio.calibration.close)
        product = root / '产品'
        product.mkdir()
        (product / 'a.max').write_bytes(b'max')
        task = studio.create({'product_dir': str(product)})
        task_file = studio.task_dir(task['id']) / 'task.json'
        before = task_file.read_bytes()

        studio.remote.settings.remember('127.0.0.1', 9999, name='坏节点', token='tok')
        studio.remote.select({'host': '127.0.0.1', 'port': 9999})
        job = studio.preview(task['id'], {})['job']
        status = read_json(studio.exchange / 'jobs' / (job['job_id'] + '.status.json'))
        status['remote']['phase'] = 'sent'
        atomic_json(studio.exchange / 'jobs' / (job['job_id'] + '.status.json'), status)

        fake_client = Mock()
        fake_client.job.return_value = {
            'state': 'done',
            'outputs': [{'path': 'task.json', 'size': 5, 'mtime_ns': 123}],
            'status': {'state': 'done', 'items': []},
        }
        with patch.object(studio.remote, 'client', return_value=fake_client):
            studio.remote.tick()

        fake_client.download.assert_not_called()
        self.assertEqual(task_file.read_bytes(), before)
        after_status = read_json(studio.exchange / 'jobs' / (job['job_id'] + '.status.json'))
        self.assertEqual(after_status['state'], 'failed')
        self.assertIn('不允许的输出路径', after_status['message'])

    def test_node_job_rejects_rooted_and_drive_relative_paths(self):
        root = test_directory(self, 'abs-re-')
        bundle = root / 'bundle'
        bundle.mkdir()
        (bundle / 'a.max').write_bytes(b'x')
        task_id = '20261126-101010-abcd'
        base = {
            'type': 'export_preview', 'job_id': '20261126-101011-abcd', 'task_id': task_id,
            'created_at': '2026-11-26T10:10:11+08:00', 'max_file': '@model:a.max',
        }
        kwargs = dict(node_exchange=root / 'ex', node_job_id='j', node_task_id='remote-1-' + task_id,
                      bundle_dir=bundle, template=root / 'tpl.max')
        for bad in (r'\Windows\System32\a.fbx', '/etc/passwd', 'C:relative.fbx', 'D:foo\\bar.fbx',
                    r'\\server\share\a.fbx', '//server/share/a.fbx', r'C:\Windows\a.fbx'):
            with self.subTest(bad=bad), self.assertRaises(ProtocolError):
                node_job({**base, 'out_fbx': bad}, **kwargs)


class RetargetRaceTests(unittest.TestCase):
    def setUp(self):
        self.config = read_json(ROOT / 'config.json')
        self.client_root = test_directory(self, 'race-client-')
        self.node_root = test_directory(self, 'race-node-')
        (self.node_root / 'template').mkdir()
        (self.node_root / 'template' / '白底模板.max').write_bytes(b'tpl')
        self.service = serve(self.node_root, host='127.0.0.1', port=0, discovery_port=0, config=self.config)
        self.addCleanup(self.service.close)
        self.studio = Studio(self.client_root, self.config)
        self.addCleanup(self.studio.calibration.close)
        product = self.client_root / '产品'
        product.mkdir()
        (product / 'a.max').write_bytes(b'max')
        self.task = self.studio.create({'product_dir': str(product)})
        code = NodeState(self.node_root).read()['code']
        node = self.studio.remote.pair('127.0.0.1', self.service.port, code)
        self.studio.remote.select(node)

    def test_select_local_skips_job_mid_send(self):
        job = self.studio.preview(self.task['id'], {})['job']
        entered = threading.Event()
        proceed = threading.Event()
        orig_put_bundle = NodeClient.put_bundle

        def slow_put_bundle(client_self, bundle, path):
            entered.set()
            proceed.wait(5)
            return orig_put_bundle(client_self, bundle, path)

        with patch.object(NodeClient, 'put_bundle', slow_put_bundle):
            worker = threading.Thread(target=self.studio.remote.tick)
            worker.start()
            self.assertTrue(entered.wait(5))
            mid_status = read_json(self.studio.exchange / 'jobs' / (job['job_id'] + '.status.json'))
            self.assertEqual(mid_status['remote']['phase'], 'sending')
            self.assertIn(job['job_id'], self.studio.remote._sending)

            # 切回本机：正在发送中的作业不能删状态，本机执行器不会重复接单
            self.studio.remote.select(None)
            self.assertTrue((self.studio.exchange / 'jobs' / (job['job_id'] + '.status.json')).is_file())
            self.assertEqual(wb_core.pending_jobs(self.studio.exchange / 'jobs'), [])

            proceed.set()
            worker.join(timeout=5)

        final_status = read_json(self.studio.exchange / 'jobs' / (job['job_id'] + '.status.json'))
        self.assertEqual(final_status['remote']['phase'], 'sent')
        self.assertNotIn(job['job_id'], self.studio.remote._sending)

    def test_send_aborts_if_status_deleted_externally_before_put_job(self):
        job = self.studio.preview(self.task['id'], {})['job']
        status_path = self.studio.exchange / 'jobs' / (job['job_id'] + '.status.json')
        orig_has_bundle = NodeClient.has_bundle

        def delete_during_check(client_self, bundle):
            status_path.unlink(missing_ok=True)
            return orig_has_bundle(client_self, bundle)

        with patch.object(NodeClient, 'has_bundle', delete_during_check), \
                patch.object(NodeClient, 'put_job') as put_job:
            self.studio.remote.tick()
            put_job.assert_not_called()
        self.assertFalse(status_path.exists())


class RenderNodeHardeningTests(unittest.TestCase):
    def setUp(self):
        self.root = test_directory(self, 'node-hard-')
        self.config = read_json(ROOT / 'config.json')
        (self.root / 'template').mkdir()
        (self.root / 'template' / '白底模板.max').write_bytes(b'tpl')

    def test_firewall_command_restricts_profile_and_program(self):
        cmd = node_setup.firewall_commands(program=r'D:\Panda Studio\PandaStudio.exe')
        self.assertNotIn('profile=any', cmd)
        self.assertIn('profile=domain,private', cmd)
        self.assertIn(r'program="D:\Panda Studio\PandaStudio.exe"', cmd)
        self.assertIn('protocol=TCP localport=8775', cmd)
        self.assertIn('protocol=UDP localport=8776', cmd)

    def test_pairing_code_ttl_rotation_and_existing_token_preserved(self):
        state = NodeState(self.root)
        first = state.ensure_code()
        self.assertRegex(first['code'], r'^\d{6}$')
        self.assertTrue(first.get('code_expires_at'))

        # 配对成功后连接码自动轮换，旧码立即失效，但旧令牌保持有效
        token1 = state.pair(first['code'], '电脑 A')
        self.assertIsNotNone(token1)
        after = state.read()
        self.assertNotEqual(after['code'], first['code'])
        self.assertIsNone(state.pair(first['code'], '重放'))
        self.assertIsNotNone(state.client(token1))

        # 连接码过期后拒绝旧码并自动换新码
        expired = state.read()
        old_code = expired['code']
        expired['code_expires_at'] = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec='seconds')
        state.write(expired)
        self.assertIsNone(state.pair(old_code, '过期尝试'))
        refreshed = state.read()
        self.assertNotEqual(refreshed['code'], old_code)
        self.assertTrue(state.code_valid(refreshed))
        self.assertIsNotNone(state.client(token1))

        # node_setup.status 暴露 code_expires_at 并在过期时刷新
        studio = Studio(self.root, self.config)
        self.addCleanup(studio.calibration.close)
        state.set_enabled(True)
        d = state.read()
        d['code_expires_at'] = (datetime.now().astimezone() - timedelta(minutes=2)).isoformat(timespec='seconds')
        stale = d['code']
        state.write(d)
        with patch.object(node_setup, 'service_running', return_value=False), \
                patch.object(node_setup, 'firewall_rule_exists', return_value=True), \
                patch.object(node_setup, 'autostart_enabled', return_value=True):
            info = node_setup.status(studio)
        self.assertNotEqual(info['code'], stale)
        self.assertTrue(info['code_expires_at'])

    def test_global_failure_cap_and_prune(self):
        node = RenderNode(self.root, self.config)
        node.state.ensure_code()
        # 放一条很旧的记录，验证会被清理
        node.failures['10.0.0.99'] = [time.time() - 9999]
        # 5 个不同 IP 各错 4 次（未触发单 IP 5 次上限），累计 20 次触发全局锁
        for ip_idx in range(5):
            ip = f'10.0.0.{ip_idx + 1}'
            for _ in range(4):
                with self.assertRaises(render_node.NodeError) as err:
                    node.pair(ip, '000000' if node.state.read()['code'] != '000000' else '111111', 'x')
                self.assertEqual(err.exception.status, 403)
        self.assertNotIn('10.0.0.99', node.failures)
        # 第 6 个全新 IP 即使输入正确连接码也被全局锁拒绝（429）
        with self.assertRaises(render_node.NodeError) as locked:
            node.pair('10.0.0.50', node.state.read()['code'], '新电脑')
        self.assertEqual(locked.exception.status, 429)

    def test_handler_timeout_concurrency_bound_and_500_redaction(self):
        node = RenderNode(self.root, self.config)
        node.state.ensure_code()
        handler_cls = node_handler(node)
        self.assertEqual(handler_cls.timeout, HANDLER_TIMEOUT_SEC)

        # 限制最大并发 = 2，发 5 个慢请求，验证峰值并发不超过 2
        active = 0
        peak = 0
        counter_lock = threading.Lock()
        orig_info = node.info

        def slow_info(client=None):
            nonlocal active, peak
            with counter_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.08)
            with counter_lock:
                active -= 1
            return orig_info(client)

        server = NodeServer(('127.0.0.1', 0), handler_cls, max_concurrent=2)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        with patch.object(node, 'info', slow_info):
            results = []

            def hit():
                results.append(NodeClient('127.0.0.1', server.server_port, timeout=5).info()['service'])

            threads = [threading.Thread(target=hit) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
        self.assertEqual(len(results), 5)
        self.assertLessEqual(peak, 2)

        # 500 错误不能把内部异常原文暴露给客户端，但必须记入内部日志
        with patch.object(node, 'info', side_effect=RuntimeError('SECRET_INTERNAL_TRACE_XYZ')):
            with self.assertRaises(RemoteError) as err:
                NodeClient('127.0.0.1', server.server_port, timeout=5).info()
        self.assertEqual(err.exception.status, 500)
        self.assertNotIn('SECRET_INTERNAL_TRACE_XYZ', str(err.exception))
        self.assertIn('SECRET_INTERNAL_TRACE_XYZ', node.log_path.read_text(encoding='utf-8'))

    def test_extract_bundle_counts_actual_decompressed_bytes(self):
        archive = self.root / 'bomb.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('a.txt', b'A')
        # zip 头里的 file_size 只有 1 字节（绕过预检），但实际解压流吐出 500 字节：分块计数必须截停并清理临时目录
        with patch.object(remote_protocol, 'EXTRACT_MAX_BYTES', 100), \
                patch.object(zipfile.ZipFile, 'open', lambda _self, _entry: io.BytesIO(b'A' * 500)):
            with self.assertRaisesRegex(ProtocolError, '太大'):
                extract_bundle(archive, self.root / 'unpacked')
        self.assertFalse((self.root / 'unpacked').exists())
        self.assertEqual(list(self.root.glob('unpacked.*.part')), [])


class KeyLeakAndSSRFTests(unittest.TestCase):
    def test_normalize_url_requires_https_for_public_allows_loopback_http(self):
        self.assertEqual(normalize_url('https://api.example.com/v1/'), 'https://api.example.com/v1')
        self.assertEqual(normalize_url('http://127.0.0.1:8080/v1/'), 'http://127.0.0.1:8080/v1')
        self.assertEqual(normalize_url('http://localhost/v1'), 'http://localhost/v1')
        self.assertEqual(normalize_url('http://[::1]:9000/v1'), 'http://[::1]:9000/v1')
        for bad in ('http://api.example.com/v1', 'http://192.168.1.10/v1', 'http://10.0.0.5/v1', 'ftp://example.com'):
            with self.subTest(bad=bad), self.assertRaises(AIError):
                normalize_url(bad)

    def test_unsaved_origin_change_does_not_leak_saved_key(self):
        root = test_directory(self, 'origin-key-')
        ai = AISettings(root)
        img = ImageAPISettings(root, ai_settings=ai)
        ai.save({'engine': 'openai_api', 'base_url': 'https://a.example/v1', 'model': 'm1', 'api_key': 'sk-ai-1111'})
        saved = img.save({'base_url': 'https://b.example/v1', 'api_key': 'sk-img-2222', 'model': 'm2', 'reuse_ai_key': False})
        cid = saved['saved']

        # 同源不同路径仍沿用已保存 Key
        self.assertEqual(ai.connection({'base_url': 'https://a.example/v2'})[1], 'sk-ai-1111')
        self.assertEqual(img.connection({'id': cid, 'base_url': 'https://b.example/v2'})[1], 'sk-img-2222')

        # 换到未保存的不同源：绝不把旧 Key 带过去
        self.assertEqual(ai.connection({'base_url': 'https://evil.example/v1'})[1], '')
        self.assertEqual(img.connection({'id': cid, 'base_url': 'https://evil.example/v1'})[1], '')
        # 手动输入了新 Key 才使用新 Key
        self.assertEqual(img.connection({'id': cid, 'base_url': 'https://evil.example/v1', 'api_key': 'sk-new-3333'})[1],
                         'sk-new-3333')
        # 调用 models 时在发网络请求前就被拦截
        with self.assertRaisesRegex(AIError, 'API Key'):
            ImageGenerator(img).models({'id': cid, 'base_url': 'https://evil.example/v1'})

    def test_download_blocks_ssrf_and_allows_same_origin_loopback(self):
        for bad in (
            'file:///C:/Windows/win.ini',
            'ftp://example.com/a.png',
            'http://127.0.0.1:8770/api/health',
            'http://[::1]:8770/a.png',
            'http://169.254.169.254/latest/meta-data',
            'http://10.0.0.1/a.png',
            'http://172.16.0.1/a.png',
            'http://192.168.1.1/a.png',
            'http://[fe80::1]/a.png',
            'http://[fc00::1]/a.png',
            'http://0.0.0.0/a.png',
            'http://224.0.0.1/a.png',
        ):
            with self.subTest(bad=bad), self.assertRaises(AIError):
                _download(bad, allow_origin='https://relay.example')

        # DNS 解析到内网 IP 同样拒绝
        fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254', 80))]
        with patch.object(image_gen.socket, 'getaddrinfo', return_value=fake_dns):
            with self.assertRaisesRegex(AIError, '禁止'):
                _download('https://rebind.example/img.png', allow_origin='https://relay.example')

        # 与配置的中转站同源的回环地址允许下载（兼容本地假中转站测试）
        body = _png_bytes()

        class ImgHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), ImgHandler)
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}/img.png'
        self.assertEqual(_download(url, allow_origin=f'http://127.0.0.1:{server.server_port}/v1'), body)
        with self.assertRaises(AIError):
            _download(url, allow_origin='http://127.0.0.1:1/v1')

    def test_post_json_and_fetcher_do_not_leak_keys_on_redirect(self):
        seen_target = []
        body = _png_bytes()

        class TargetHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def handle_any(self):
                seen_target.append({
                    'path': self.path,
                    'auth': self.headers.get('Authorization'),
                    'goog': self.headers.get('x-goog-api-key'),
                })
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = handle_any

        target_srv = ThreadingHTTPServer(('127.0.0.1', 0), TargetHandler)
        threading.Thread(target=target_srv.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True).start()
        self.addCleanup(target_srv.server_close)
        self.addCleanup(target_srv.shutdown)

        class RedirectHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def handle_any(self):
                if self.path == '/same-img':
                    location = f'http://127.0.0.1:{redir_srv.server_port}/final-img'
                elif self.path == '/final-img':
                    seen_target.append({
                        'path': self.path,
                        'auth': self.headers.get('Authorization'),
                        'goog': self.headers.get('x-goog-api-key'),
                    })
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/png')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                else:
                    location = f'http://127.0.0.1:{target_srv.server_port}/steal'
                self.send_response(307)
                self.send_header('Location', location)
                self.send_header('Content-Length', '0')
                self.end_headers()

            do_GET = do_POST = handle_any

        redir_srv = ThreadingHTTPServer(('127.0.0.1', 0), RedirectHandler)
        threading.Thread(target=redir_srv.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True).start()
        self.addCleanup(redir_srv.server_close)
        self.addCleanup(redir_srv.shutdown)

        base = f'http://127.0.0.1:{redir_srv.server_port}/v1'
        # 1. _post_json 拒绝重定向，不把 Key 发给跳转目标
        with self.assertRaisesRegex(AIError, '跳转'):
            _post_json(base + '/models/m:generateContent', {'a': 1}, 'sk-secret-key')
        self.assertEqual(seen_target, [])

        # 2. _fetcher json_reply=True 拒绝重定向
        fetch = _fetcher(base, 'sk-secret-key')
        with self.assertRaisesRegex(AIError, '跳转'):
            fetch('/v1/images/tasks/t1', json_reply=True)
        self.assertEqual(seen_target, [])

        # 3. _fetcher 图片下载跳转到不同本地端口（非同源回环）被 SSRF 拦截
        with self.assertRaisesRegex(AIError, '禁止'):
            fetch('/other-port', json_reply=False)
        self.assertEqual(seen_target, [])

        # 4. _fetcher 图片下载在允许跳转时必须剥离 Authorization 头
        got = fetch('/same-img', json_reply=False)
        self.assertEqual(got, body)
        self.assertEqual(len(seen_target), 1)
        self.assertIsNone(seen_target[0]['auth'])
        self.assertIsNone(seen_target[0]['goog'])


class SecretsAndDesktopStorageTests(unittest.TestCase):
    def test_secrets_default_under_root_and_legacy_migration_verifies_bytes(self):
        root = test_directory(self, 'secrets-root-')
        ai = AISettings(root)
        img = ImageAPISettings(root, ai_settings=ai)
        ai.save({'api_key': 'sk-local-root-1234', 'base_url': 'https://a.example/v1'})
        saved = img.save({'api_key': 'sk-img-root-5678', 'base_url': 'https://b.example/v1'})
        cid = saved['saved']
        self.assertTrue((root / 'secrets' / 'ai_key.bin').is_file())
        self.assertTrue((root / 'secrets' / 'image_keys' / f'{cid}.bin').is_file())

        # 模拟旧版 %APPDATA%\whitebg-studio 迁移到 <target>\secrets
        legacy_parent = test_directory(self, 'legacy-appdata-')
        legacy = legacy_parent / 'whitebg-studio'
        target_secrets = test_directory(self, 'migrated-secrets-') / 'secrets'
        (legacy / 'image_keys').mkdir(parents=True)
        ai_blob = dpapi(b'sk-old-ai-9999')
        ch_blob = dpapi(b'sk-old-ch-8888')
        (legacy / 'ai_key.bin').write_bytes(ai_blob)
        (legacy / 'image_keys' / '11223344.bin').write_bytes(ch_blob)

        # 写入校验失败时保留原文件
        with patch.object(Path, 'read_bytes', side_effect=[ai_blob, b'corrupted', ch_blob, b'corrupted']):
            moved = migrate_legacy_secrets(target_secrets, legacy)
        self.assertEqual(moved, 0)
        self.assertTrue((legacy / 'ai_key.bin').is_file())
        self.assertTrue((legacy / 'image_keys' / '11223344.bin').is_file())

        # 正常迁移：字节一致后才删除原文件，空目录清理
        moved = migrate_legacy_secrets(target_secrets, legacy)
        self.assertEqual(moved, 2)
        self.assertEqual((target_secrets / 'ai_key.bin').read_bytes(), ai_blob)
        self.assertEqual((target_secrets / 'image_keys' / '11223344.bin').read_bytes(), ch_blob)
        self.assertFalse(legacy.exists())

        # .gitignore 包含 secrets/ 和 webview-data/
        gitignore = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
        self.assertIn('secrets/', gitignore)
        self.assertIn('webview-data/', gitignore)

    def test_desktop_webview_uses_root_storage_and_persists_localstorage(self):
        loader = importlib.machinery.SourceFileLoader('whitebg_desktop_sec', str(ROOT / 'desktop.pyw'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        desktop = importlib.util.module_from_spec(spec)
        loader.exec_module(desktop)

        temp_root = test_directory(self, 'desk-storage-')
        self.assertEqual(desktop.webview_storage_dir(temp_root), temp_root / 'webview-data')

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
        fake_webview.create_window.return_value = fake_window
        with patch.dict(sys.modules, {'webview': fake_webview}), \
                patch.object(desktop, 'ROOT', temp_root), \
                patch.object(desktop, 'ensure_max_exe'), \
                patch.object(desktop, 'refresh_max_plugin'), \
                patch.object(desktop, 'start_service', return_value=(None, None)), \
                patch.object(desktop, 'stop_service'), \
                patch.object(desktop.ctypes.windll.user32, 'MessageBoxW') as msgbox, \
                patch('server.node_setup.ensure_service'), \
                patch.object(sys, 'argv', ['desktop.pyw']):
            rc = desktop.main()
        msgbox.assert_not_called()
        self.assertEqual(rc, 0)
        fake_webview.start.assert_called_once_with(
            gui='edgechromium',
            private_mode=False,
            storage_path=str(temp_root / 'webview-data'),
        )
        self.assertTrue((temp_root / 'webview-data').is_dir())


class RunnerGraceAndCalibrationTimeoutTests(unittest.TestCase):
    def test_runner_long_render_grace_only_for_active_rendering_job(self):
        root = test_directory(self, 'runner-grace-')
        exchange = root / 'exchange'
        (exchange / 'jobs').mkdir(parents=True)
        runner = RunnerStatus(root, exchange, {})
        atomic_json(exchange / 'runner.heartbeat.json', {
            'epoch': 1000, 'busy': True, 'job_id': '20261126-101010-abcd', 'runner_version': '0.2'
        })
        # 无作业状态文件：按原 30 分钟宽限，超时即离线
        with patch.object(runner, '_probe_max', return_value=True), \
                patch('server.runner.time.time', return_value=1000 + BUSY_GRACE_SEC + 10):
            self.assertFalse(runner.status()['online'])
        # 有活跃 rendering 作业状态文件：4 小时内仍在线
        atomic_json(exchange / 'jobs' / '20261126-101010-abcd.status.json', {
            'job_id': '20261126-101010-abcd', 'state': 'running',
            'items': [{'no': 1, 'state': 'rendering'}],
        })
        with patch.object(runner, '_probe_max', return_value=True), \
                patch('server.runner.time.time', return_value=1000 + 3600):
            self.assertTrue(runner.status()['online'])
        # 超过 4 小时或作业完成后不再宽限
        with patch.object(runner, '_probe_max', return_value=True), \
                patch('server.runner.time.time', return_value=1000 + RENDERING_GRACE_SEC + 10):
            self.assertFalse(runner.status()['online'])
        atomic_json(exchange / 'jobs' / '20261126-101010-abcd.status.json', {
            'job_id': '20261126-101010-abcd', 'state': 'done',
            'items': [{'no': 1, 'state': 'done'}],
        })
        with patch.object(runner, '_probe_max', return_value=True), \
                patch('server.runner.time.time', return_value=1000 + 3600):
            self.assertFalse(runner.status()['online'])

    def test_calibration_await_round_timeout_and_lock_not_held_during_wait(self):
        root = test_directory(self, 'cal-timeout-')
        config = read_json(ROOT / 'config.json')
        config.update(calibration_auto=True, calibration_timeout_sec=0.2,
                      smallshot_size={'portrait': [60, 80], 'landscape': [80, 60]})
        studio = Studio(root, config)
        self.addCleanup(studio.calibration.close)
        product = root / '产品'
        product.mkdir()
        (product / 'a.max').write_text('x', encoding='utf-8')
        task = studio.create({'product_dir': str(product)})

        job = studio.submit(task['id'], {'mode': 'smallshot'})
        # 等待期间 studio.lock 不能一直被校准线程占住：主线程应可立即拿到锁
        acquired = studio.lock.acquire(timeout=0.1)
        self.assertTrue(acquired)
        studio.lock.release()

        # 等待超时触发（没有任何执行器响应）
        studio.calibration.thread.join(timeout=3)
        self.assertIsNone(studio.calibration.active)
        after = studio.load(task['id'])
        self.assertEqual(after['ai_calibration']['state'], 'failed')
        self.assertEqual(after['ai_calibration']['rounds'][-1]['error'], WAIT_TIMEOUT_MSG)
        # 作业状态被标成 failed，任务恢复可编辑/可重新提交状态
        status = read_json(studio.exchange / 'jobs' / (job['job_id'] + '.status.json'))
        self.assertEqual(status['state'], 'failed')
        updated = studio.update(task['id'], {'lighting': {'azimuth_deg': 20}})
        self.assertEqual(updated['lighting']['azimuth_deg'], 20)

    def test_calibration_detects_vanished_job_file(self):
        root = test_directory(self, 'cal-vanish-')
        config = read_json(ROOT / 'config.json')
        config.update(calibration_auto=True, calibration_timeout_sec=30,
                      smallshot_size={'portrait': [60, 80], 'landscape': [80, 60]})
        studio = Studio(root, config)
        self.addCleanup(studio.calibration.close)
        product = root / '产品'
        product.mkdir()
        (product / 'a.max').write_text('x', encoding='utf-8')
        task = studio.create({'product_dir': str(product)})

        job = studio.submit(task['id'], {'mode': 'smallshot'})
        (studio.exchange / 'jobs' / (job['job_id'] + '.json')).unlink()
        studio.calibration.thread.join(timeout=3)
        self.assertIsNone(studio.calibration.active)
        after = studio.load(task['id'])
        self.assertEqual(after['ai_calibration']['state'], 'failed')
        self.assertEqual(after['ai_calibration']['rounds'][-1]['error'], WAIT_TIMEOUT_MSG)


if __name__ == '__main__':
    unittest.main()
