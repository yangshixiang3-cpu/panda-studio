"""通过真实 localhost HTTP 和隔离密钥路径验证中转站校准。"""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sys
from uuid import uuid4
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_relay_fixture import KEY, MODEL, COLOR, TOKENS, completion, decode_image, normal_reply, serve
from server import ai_client
from server.ai_client import OpenAIClient, PROBE_SCHEMA, round_images
from server.ai_settings import AIError, AISettings, dpapi
from server.app import LocalServer, handler_for
from server.calibration import validate_schema
from server.core import Studio, read_json
from tools.fake_runner import run_once
from fixture_files import needs_product_fbx


class FakeRelay:
    def __init__(self, reply=normal_reply, *, redirect_to=None):
        self.reply = reply
        self.redirect_to = redirect_to
        self.requests = []
        self.headers = []
        self.rejected = 0

    @contextmanager
    def running(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def dispatch(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.command == 'POST' else None
                relay.headers.append(self.headers)
                relay.requests.append((self.path, payload))
                if not self.headers.get('User-Agent', '').startswith('OpenAI/Python'):
                    relay.rejected += 1
                    status, value = 403, b'<html><title>Cloudflare</title>Error 1010: Access denied</html>'
                elif self.headers.get('Authorization') != 'Bearer '+KEY:
                    status, value = 401, {'error': 'API Key 无效'}
                elif relay.redirect_to:
                    status, value = 307, b''
                else:
                    status, value = relay.reply(self.path, payload)
                    if status == 200 and isinstance(value, dict):
                        if self.path.endswith('/models'):
                            value = {'object': 'list', 'data': [
                                {'object': 'model', 'created': 0, 'owned_by': 'fake-relay', **entry}
                                for entry in value['data']]}
                        else:
                            value = {'id': 'chatcmpl-local', 'object': 'chat.completion', 'created': 0,
                                     'model': payload['model'], **value, 'choices': [
                                         {'index': index, 'finish_reason': 'stop',
                                          'message': {'role': 'assistant', **entry['message']}}
                                         for index, entry in enumerate(value['choices'])]}
                data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
                try:
                    self.send_response(status)
                    if status == 307:
                        self.send_header('Location', relay.redirect_to)
                    self.send_header('Content-Type', 'text/html' if status == 403 and isinstance(value, bytes) else 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            do_GET = dispatch
            do_POST = dispatch

        with serve(ThreadingHTTPServer(('127.0.0.1', 0), Handler)) as server:
            self.url = f'http://127.0.0.1:{server.server_port}'
            yield self


def temp_root():
    directory = ROOT/'tests'/'tmp'/('ai-api-'+uuid4().hex)
    directory.mkdir(parents=True)
    return directory


def prepare_studio(root):
    fixture = ROOT/'tests'/'fixtures'/'L型咖啡柜_绑定.fbx'
    config = read_json(ROOT/'config.json')
    config.update(exchange_dir='exchange', max_exe='', calibration_auto=False,
                  codex_command=[], calibration_max_rounds=1,
                  smallshot_size={'portrait': [240, 320], 'landscape': [320, 240]})
    studio = Studio(root, config)
    studio.calibration.ai_settings = AISettings(root, root/'keys'/'ai_key.bin')
    product = root/'测试咖啡柜'
    (product/'模型').mkdir(parents=True)
    (product/'模型'/'开发占位.max').write_text('仅供假执行器测试。', encoding='utf-8')
    shutil.copy2(fixture, product/'模型'/fixture.name)
    return studio, product, fixture


class SecretStorageTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', '需要 Windows DPAPI')
    def test_real_dpapi_round_trip_in_injected_test_path(self):
        root = temp_root()
        key_path = root/'win-user'/'ai_key.bin'
        store = AISettings(root, key_path)
        self.assertEqual(store.public()['engine'], 'codex')
        public = store.save({'api_key': KEY, 'base_url': 'http://localhost/custom///'})
        self.assertTrue(public['key_set'])
        self.assertEqual(public['key_tail'], '3517')
        self.assertEqual(public['base_url'], 'http://localhost/custom')
        self.assertEqual(dpapi(key_path.read_bytes(), decrypt=True), KEY.encode())
        self.assertNotIn(KEY.encode(), key_path.read_bytes())
        encrypted = key_path.read_bytes()
        self.assertTrue(store.save({'api_key': ''})['key_set'])
        self.assertEqual(key_path.read_bytes(), encrypted)
        self.assertNotIn(KEY, store.path.read_text(encoding='utf-8'))
        with self.assertRaisesRegex(AIError, '不能包含 API Key') as failure:
            store.save({'model': KEY})
        self.assertNotIn(KEY, str(failure.exception))
        for url in ('file:///tmp/a', 'http://key@example.test', 'http://example.test/a?secret=1'):
            with self.subTest(url=url), self.assertRaises(AIError):
                store.save({'base_url': url})

    def test_non_windows_refuses_to_encrypt(self):
        with patch('server.ai_settings.os.name', 'posix'), self.assertRaisesRegex(AIError, '仅支持 Windows 存储密钥'):
            dpapi(b'fake')


class RelayTests(unittest.TestCase):
    def configure(self, relay, *, path='/custom/v1'):
        root = temp_root()
        store = AISettings(root, root/'ai_key.bin')
        if os.name != 'nt':
            mock = patch('server.ai_settings.dpapi', lambda data, **_: data[::-1])
            mock.start()
            self.addCleanup(mock.stop)
        store.save({'engine': 'openai_api', 'base_url': relay.url+path+'/', 'model': MODEL, 'api_key': KEY})
        return store

    def analyze(self, store):
        client = OpenAIClient(store)
        result = client.analyze([], '图片校准规则', '测试数据', PROBE_SCHEMA, validate_schema)
        self.assertEqual(result, COLOR)
        return client

    def test_models_sort_and_address_path(self):
        with FakeRelay().running() as relay:
            store = self.configure(relay, path='/custom')
            self.assertEqual(OpenAIClient(store).models(), [MODEL, 'a-model', 'z-model'])
            self.assertEqual(relay.requests, [('/custom/models', None)])
            self.assertTrue(relay.headers[0]['User-Agent'].startswith('OpenAI/Python'))

    def test_cloudflare_gate_rejects_other_request_identity(self):
        with FakeRelay().running() as relay:
            request = Request(relay.url+'/custom/v1/models', headers={'Authorization': f'Bearer {KEY}'})
            with self.assertRaises(HTTPError) as failure:
                urlopen(request, timeout=5)
            with failure.exception as response:
                self.assertEqual(response.code, 403)
                self.assertIn(b'Error 1010', response.read())
            self.assertEqual(relay.rejected, 1)

    def test_sdk_request_identity_passes_cloudflare_gate(self):
        with FakeRelay(lambda *_: completion(COLOR)).running() as relay:
            self.analyze(self.configure(relay))
            self.assertEqual(relay.rejected, 0)
            self.assertTrue(relay.headers[-1]['User-Agent'].startswith('OpenAI/Python'))

    def test_sdk_does_not_follow_redirects(self):
        with FakeRelay().running() as other:
            target = other.url.replace('127.0.0.1', 'localhost')
            with FakeRelay(redirect_to=target+'/models').running() as relay:
                client = OpenAIClient(self.configure(relay))
                with self.assertRaisesRegex(AIError, '中转站要求跳转，请填写最终接口地址'):
                    client.models()
                self.assertEqual(len(relay.requests), 1)
                self.assertEqual(other.requests, [])

    def test_structured_levels_and_cache_per_address_and_model(self):
        for supported in ('json_schema', 'json_object', 'text'):
            with self.subTest(supported=supported):
                def reply(path, payload):
                    mode = payload.get('response_format', {}).get('type', 'text')
                    if mode != supported:
                        return 400, {'error': {'message': KEY+' unsupported response_format: '+mode}}
                    return completion(COLOR, fences=True)

                with FakeRelay(reply).running() as relay:
                    store = self.configure(relay)
                    self.assertEqual(self.analyze(store).mode, supported)
                    self.assertEqual(store.public()['structured_mode'], supported)
                    modes = ('json_schema', 'json_object', 'text')
                    used = [request.get('response_format', {}).get('type', 'text') for _, request in relay.requests]
                    self.assertEqual(used, list(modes[:modes.index(supported)+1]))
                    request = relay.requests[0][1]
                    self.assertEqual(request['response_format']['json_schema'],
                                     {'name': 'calibration', 'schema': PROBE_SCHEMA, 'strict': True})
                    self.assertEqual(request['messages'][0], {'role': 'system', 'content': '图片校准规则'})
                    if supported == 'text':
                        text = relay.requests[-1][1]['messages'][1]['content'][0]['text']
                        self.assertTrue(text.startswith('测试数据\n只输出符合以下 schema 的 JSON'))
                        self.assertEqual(json.loads(text.split('\n')[-1]), PROBE_SCHEMA)
                    fresh = AISettings(store.path.parent, store.key_path)
                    self.assertEqual(fresh.connection({'model': 'another'})[2], {})
                    self.assertEqual(fresh.connection({'base_url': relay.url+'/other'})[2], {})
                    count = len(relay.requests)
                    self.assertEqual(self.analyze(fresh).mode, supported)
                    self.assertEqual(len(relay.requests), count+1)

    def test_effort_fallback_and_omission(self):
        def reply(path, payload):
            if 'reasoning_effort' in payload:
                return 400, {'error': {'param': 'reasoning_effort', 'message': 'Unknown parameter'}}
            return completion(COLOR)

        with FakeRelay(reply).running() as relay:
            store = self.configure(relay)
            self.analyze(store)
            self.analyze(AISettings(store.path.parent, store.key_path))
            self.assertEqual([p.get('reasoning_effort') for _, p in relay.requests], ['medium', None, None])
            self.assertTrue(store.connection()[2]['omit_effort'])
            store.save({'model': 'another', 'reasoning_effort': None})
            self.analyze(store)
            self.assertNotIn('reasoning_effort', relay.requests[-1][1])

    def test_http_errors_and_redaction(self):
        for status, upstream, message in (
            (401, 'Unauthorized', 'API Key 无效或没有权限'),
            (403, 'Forbidden', r'API Key 没有权限，或中转站拒绝了请求（HTTP 403）'),
            (403, 'Error 1010: Access denied cloudflare', r'中转站的防护（Cloudflare）拦截了请求'),
            (404, 'Missing', '中转站没有这个模型'),
            (400, 'model_not_found', '中转站没有这个模型'),
            (400, 'unrelated', '中转站拒绝请求'),
            (429, 'Too many requests', '中转站限流或额度不足，请稍后再试'),
        ):
            with self.subTest(status=status), FakeRelay(lambda *_: (status, {'error': upstream+' '+KEY})).running() as relay:
                store = self.configure(relay)
                with patch.object(ai_client.time, 'sleep') as sleep, self.assertRaisesRegex(AIError, message) as failure:
                    self.analyze(store)
                self.assertNotIn(KEY, str(failure.exception))
                self.assertEqual(len(relay.requests), 3 if status == 429 else 1)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 6] if status == 429 else [])

    def test_retry_429_and_5xx_then_success(self):
        outcomes = iter([(429, {}), (503, {}), completion(COLOR)])
        with FakeRelay(lambda *_: next(outcomes)).running() as relay:
            with patch.object(ai_client.time, 'sleep') as sleep:
                self.analyze(self.configure(relay))
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 6])
            self.assertEqual(len(relay.requests), 3)

    def test_timeout_and_unreachable(self):
        import threading
        release = threading.Event()

        def wait_then_reply(*_):
            release.wait(.4)
            return completion(COLOR)

        with FakeRelay(wait_then_reply).running() as relay:
            store = self.configure(relay)
            self.assertEqual(ai_client.TIMEOUT, 180)
            with patch.object(ai_client, 'TIMEOUT', .07), patch.object(ai_client, 'BACKOFF', (0, 0)):
                with self.assertRaisesRegex(AIError, '中转站响应超时'):
                    self.analyze(store)
            self.assertEqual(len(relay.requests), 3)
            release.set()
        with socket.socket() as unused:
            unused.bind(('127.0.0.1', 0))
            store.save({'base_url': f'http://127.0.0.1:{unused.getsockname()[1]}'})
            with patch.object(ai_client, 'BACKOFF', (0, 0)), self.assertRaisesRegex(AIError, '连不上中转站，请检查地址和网络'):
                self.analyze(store)

    def test_invalid_json_and_schema_retry_once(self):
        for wrong in (completion({'red': 200}), completion({**COLOR, 'blue': -4}), (200, b'not json')):
            with self.subTest(wrong=wrong), FakeRelay(lambda *_: wrong).running() as relay:
                store = self.configure(relay)
                with self.assertRaisesRegex(AIError, '模型没有按要求返回结果'):
                    self.analyze(store)
                self.assertEqual(len(relay.requests), 2)
                self.assertIsNone(store.public()['structured_mode'])
        outcomes = iter([completion({**COLOR, 'blue': '错'}), completion(COLOR, fences=True)])
        with FakeRelay(lambda *_: next(outcomes)).running() as relay:
            client = self.analyze(self.configure(relay))
            self.assertEqual(client.tokens['total_tokens'], 2*TOKENS['total_tokens'])

    def test_image_encoding_and_size(self):
        path = temp_root()/'region.png'
        colors = ((200, 60, 40), (10, 20, 30))
        image = Image.new('RGB', (2000, 100), colors[0])
        image.paste(colors[1], (1000, 0, 2000, 100))
        image.save(path)
        urls = round_images([path, path], [{'role': '材质分区图'}, {'role': '小样'}])
        for url, mime in zip(urls, ('png', 'jpeg')):
            header, decoded = decode_image({'image_url': {'url': url}})
            self.assertEqual(header, 'data:image/'+mime+';base64')
            self.assertEqual(decoded.size, (1600, 80))
            if mime == 'png':
                self.assertEqual({color for _, color in decoded.getcolors(2)}, set(colors))

    @needs_product_fbx
    def test_complete_calibration_and_no_key_in_responses_or_files(self):
        root = temp_root()
        studio, product, fixture = prepare_studio(root)
        if os.name != 'nt':
            mock = patch('server.ai_settings.dpapi', lambda data, **_: data[::-1])
            mock.start()
            self.addCleanup(mock.stop)
        out, err = io.StringIO(), io.StringIO()
        unauthorized = False

        def reply(path, payload):
            if unauthorized:
                return 401, {'error': 'key echoed '+KEY}
            status, response = normal_reply(path, payload)
            if payload is not None:
                message = response['choices'][0]['message']
                result = json.loads(message['content'])
                field = 'summary' if 'summary' in result else 'description'
                result[field] += ' '+KEY
                return completion(result)
            return status, response

        response_count = 0
        with FakeRelay(reply).running() as relay, redirect_stdout(out), redirect_stderr(err):
            with serve(LocalServer(('127.0.0.1', 0), handler_for(studio))) as server:
                def api(path, data=None, *, expected=200, origin=None):
                    nonlocal response_count
                    headers = {'Content-Type': 'application/json'} if data is not None else {}
                    if origin:
                        headers['Origin'] = origin
                    addr = f'http://127.0.0.1:{server.server_port}'
                    request = Request(addr+path, data=json.dumps(data).encode() if data is not None else None,
                                      headers=headers)
                    try:
                        response = urlopen(request, timeout=10)
                    except HTTPError as error:
                        response = error
                    with response:
                        raw = response.read()
                        self.assertNotIn(KEY.encode(), raw, path)
                        self.assertEqual(response.code, expected, raw)
                        response_count += 1
                        return json.loads(raw)

                self.assertEqual(api('/api/ai/settings')['engine'], 'codex')
                for endpoint in ('settings', 'models', 'test'):
                    api('/api/ai/'+endpoint, {}, origin='http://invalid.test', expected=403)
                api('/api/ai/settings', {'engine': 'openai_api', 'base_url': relay.url+'/v1',
                                         'model': MODEL, 'api_key': KEY})
                self.assertEqual(api('/api/ai/settings')['key_tail'], '3517')
                self.assertEqual(api('/api/ai/models', {})['models'][0], MODEL)
                probe = api('/api/ai/test', {})
                self.assertTrue(probe['connected'] and probe['vision'])
                self.assertEqual(probe['structured_mode'], 'json_schema')
                unauthorized = True
                self.assertEqual(api('/api/ai/models', {}, expected=400)['error'], 'API Key 无效或没有权限')
                self.assertEqual(api('/api/ai/test', {})['reason'], 'API Key 无效或没有权限')
                unauthorized = False
                task = api('/api/tasks', {'product_dir': str(product)}, expected=201)['task']
                task_api = '/api/tasks/'+task['id']

                def finish():
                    deadline = time.monotonic()+18
                    while time.monotonic() < deadline:
                        run_once(studio.exchange, fixture)
                        finished = api(task_api)['task']
                        if finished['ai_calibration']['state'] in ('failed', 'waiting_user'):
                            return finished
                        time.sleep(.12)
                    self.fail('等待假执行器和校准完成超时')

                api(task_api+'/calibration', {'action': 'start'})
                task = finish()
                calibration = task['ai_calibration']
                self.assertEqual(calibration['state'], 'waiting_user', calibration)
                rounds = calibration['rounds']
                self.assertEqual([(r['n'], r['verdict']) for r in rounds], [(0, 'adjust'), (1, 'ok')])
                self.assertTrue(any(v.get('brightness_mul') == 1.18 for v in rounds[1]['overrides']['materials'].values()))
                for record in rounds:
                    usage = record['usage']
                    folder = studio.task_dir(task['id'])/'calibration'/f"round-{record['n']}"
                    self.assertEqual(usage, read_json(folder/'usage.json'))
                    self.assertEqual(usage['engine'], 'openai_api')
                    self.assertEqual(usage['model'], MODEL)
                    self.assertGreaterEqual(usage['latency_sec'], 0)
                    self.assertEqual(usage['total_tokens'], 12300)
                    self.assertIn('***', record['summary'])
                    self.assertFalse((folder/'command.json').exists())
                chosen = api(task_api+'/calibration', {'action': 'accept', 'round': 1})['task']
                self.assertEqual(chosen['ai_calibration']['accepted_overrides'], rounds[1]['overrides'])
                unauthorized = True
                api(task_api+'/calibration', {'action': 'retry'})
                failed = finish()['ai_calibration']
                self.assertEqual(failed['state'], 'failed')
                self.assertEqual(failed['accepted_round'], 1)
                last = failed['rounds'][-1]
                message = 'AI 校准失败，可直接用当前小样。API Key 无效或没有权限'
                self.assertEqual(last['error'], message)
                folder = studio.task_dir(task['id'])/'calibration'/'round-2'
                self.assertTrue(list((folder/'smallshots').glob('*.jpg')))
                self.assertEqual((folder/'error.log').read_text(encoding='utf-8'), message)
        self.assertGreater(response_count, 10)
        self.assertIn('假执行器', out.getvalue())
        self.assertIn('本地请求', out.getvalue())
        (root/'service-and-runner.log').write_text(out.getvalue()+err.getvalue(), encoding='utf-8')
        for path in root.rglob('*'):
            if path.is_file():
                self.assertNotIn(KEY.encode(), path.read_bytes(), str(path))


class SDKAvailabilityTests(unittest.TestCase):
    @needs_product_fbx
    def test_sdk_unavailable_keeps_settings_and_workbench_available(self):
        root = temp_root()
        studio, _, _ = prepare_studio(root)
        broken_libs = root/'broken-pylibs'
        sdk_file = broken_libs/'openai'/'__init__.py'
        sdk_file.parent.mkdir(parents=True)
        sdk_file.touch()
        with serve(LocalServer(('127.0.0.1', 0), handler_for(studio))) as server:
            base_url = f'http://127.0.0.1:{server.server_port}'

            def api(path, data=None):
                request = Request(base_url+path, data=None if data is None else json.dumps(data).encode(),
                                  headers={'Content-Type': 'application/json'})
                with urlopen(request, timeout=5) as response:
                    return json.load(response)

            for libs in (root/'missing-pylibs', broken_libs):
                with self.subTest(libs=libs.name), patch.object(ai_client, 'PYLIBS', libs), \
                        patch.object(ai_client, 'import_module', side_effect=ImportError('模拟导入失败')):
                    self.assertEqual(api('/api/ai/settings')['engine'], 'codex')
                    self.assertEqual(api('/api/ai/settings', {'model': MODEL})['model'], MODEL)
                    result = api('/api/ai/test', {})
                    self.assertFalse(result['connected'])
                    self.assertEqual(result['reason'], ai_client.MISSING_SDK)
                    with self.assertRaises(HTTPError) as failure:
                        api('/api/ai/models', {})
                    with failure.exception as response:
                        self.assertEqual(response.code, 400)
                        self.assertEqual(json.load(response)['error'], ai_client.MISSING_SDK)
                    self.assertEqual(api('/api/health')['service'], 'whitebg-studio')
                    self.assertEqual(api('/api/tasks')['tasks'], [])

    def test_incompatible_python_reports_version(self):
        root = temp_root()
        sdk_file = root/'openai'/'__init__.py'
        sdk_file.parent.mkdir()
        sdk_file.touch()
        with patch.object(ai_client, 'PYLIBS', root), \
                patch.object(ai_client, 'import_module', side_effect=ImportError('pydantic_core._pydantic_core')), \
                patch.object(sys, 'version_info', (3, 12, 0)), \
                patch.object(ai_client.platform, 'python_version', return_value='3.12.0'):
            with self.assertRaisesRegex(AIError, r'需要 Python 3.13（64 位），当前是 Python 3.12.0'):
                ai_client.load_sdk()
