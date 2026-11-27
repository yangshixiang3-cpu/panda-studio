"""画布：保存、图片文件、生图接口（图片接口 / 对话接口）、失败处理。用本机假的生图接口，不花钱。"""
import base64
import io
import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.ai_settings import AIError, AISettings, dpapi
from server.canvas import CanvasError, CanvasStore
from server.core import Studio, read_json
from server.image_gen import (ImageAPISettings, ImageGenerator, api_root, extract_chat_image, image_size, quality_param,
                              resolve_mode, size_params)
from test_temp_dirs import test_directory

KEY = 'sk-fake-canvas-2468'


def png(color='red', size=(64, 48)):
    stream = io.BytesIO()
    Image.new('RGB', size, color).save(stream, 'PNG')
    return stream.getvalue()


class FakeImageAPI:
    def __init__(self):
        self.requests = []
        self.status = 200
        self.images_reply = None          # 设了就用它当图片接口的返回（模拟各种中转站写法）
        self.tasks = {}                   # 异步任务号 → 依次返回的查询结果（最后一个一直返回）
        self.images_refused = False       # 图片接口回 404「Images API is not supported」（如中转站的 Gemini）
        self.gets = []

    def __enter__(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                api.gets.append({'path': self.path, 'auth': self.headers.get('Authorization')})
                if '/images/tasks/' in self.path:
                    replies = api.tasks.get(self.path.rsplit('/', 1)[-1].split('?')[0])
                    if not replies:
                        return self.reply(404, {'error': {'message': 'no task'}})
                    return self.reply(200, replies.pop(0) if len(replies) > 1 else replies[0])
                if self.path.endswith('/img.png'):
                    body = png('purple')
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/png')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
                if self.path.endswith('/models'):
                    return self.reply(200, {'object': 'list', 'data': [{'id': 'gpt-6.2-sol'}, {'id': 'gpt-image-1'},
                                                                         {'id': 'gemini-2.5-flash-image'}]})
                self.reply(404, {'error': {'message': 'no'}})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                api.requests.append({'path': self.path, 'auth': self.headers.get('Authorization'),
                                     'type': self.headers.get('Content-Type', ''), 'body': body,
                                     'idempotency': self.headers.get('Idempotency-Key', '')})
                if api.status != 200:
                    return self.reply(api.status, {'error': {'message': 'bad key'}})
                image = base64.b64encode(png('blue')).decode()
                if api.images_refused and '/images/' in self.path:
                    return self.reply(404, {'error': {'message': 'Images API is not supported for this platform'}})
                if api.images_reply is not None and '/images/' in self.path:
                    return self.reply(200, api.images_reply)
                if self.path.endswith('/images/generations') or self.path.endswith('/images/edits'):
                    return self.reply(200, {'created': 1, 'data': [{'b64_json': image}]})
                if self.path.endswith(':generateContent'):
                    return self.reply(200, {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
                        {'text': '好的'}, {'inlineData': {'mimeType': 'image/png', 'data': image}}]}}]})
                if self.path.endswith('/chat/completions'):
                    return self.reply(200, {'id': 'x', 'object': 'chat.completion', 'created': 1, 'model': 'm',
                                            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
                                                'role': 'assistant', 'content': f'好的 ![图](data:image/png;base64,{image})'}}]})
                self.reply(404, {'error': {'message': 'no'}})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()


class CanvasTests(unittest.TestCase):
    def setUp(self):
        self.root = test_directory(self, 'canvas-')
        self.studio = Studio(self.root, read_json(ROOT / 'config.json'))
        self.addCleanup(self.studio.calibration.close)
        self.ai = AISettings(self.root, self.root / 'keys' / 'ai_key.bin')
        self.settings = ImageAPISettings(self.root, self.root / 'keys' / 'image_key.bin', self.ai)
        self.canvas = CanvasStore(self.studio, self.settings)
        self.pid = self.canvas.list_projects()['projects'][0]['id']        # 没有项目时自动建「我的项目」
        self.api = FakeImageAPI().__enter__()
        self.addCleanup(self.api.__exit__)

    def wait(self, run_id):
        deadline = time.time() + 20
        while time.time() < deadline:
            record = self.canvas.run(run_id)
            if record['state'] != 'running':
                return record
            time.sleep(.05)
        self.fail('生成一直没结束')

    def test_settings_reuse_ai_relay_key_by_default(self):
        self.ai.save({'engine': 'openai_api', 'base_url': self.api.url, 'model': 'gpt-6.2-sol', 'api_key': KEY})
        base_url, key, model, mode = self.settings.connection()
        self.assertEqual((base_url, key, mode), (self.api.url, KEY, 'auto'))
        public = self.settings.save({'model': 'gpt-image-1'})
        cid = public['saved']
        self.assertEqual(public['channels'][0]['key_source'], 'ai')
        self.assertNotIn(KEY, json.dumps(public))
        self.settings.save({'id': cid, 'api_key': 'sk-own-1357'})
        self.assertEqual(self.settings.connection()[1], 'sk-own-1357')
        self.assertEqual(self.settings.public()['channels'][0]['key_source'], 'own')
        self.settings.save({'id': cid, 'clear_key': True, 'reuse_ai_key': False})
        self.assertEqual(self.settings.connection()[1], '')

    def test_images_api_generate_with_and_without_refs(self):
        self.settings.save({'base_url': self.api.url, 'api_key': KEY, 'model': 'gpt-image-1'})
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '奶油白边柜', 'ratio': '3:4', 'count': 2, 'node': '3',
                                                 'quality': 'high', 'resolution': '4K'})['id'])
        self.assertEqual(record['state'], 'done', record['error'])
        self.assertEqual(len(record['files']), 2)
        for entry in record['files']:
            self.assertTrue(self.canvas.file_path(self.pid, entry['file']).is_file())
            self.assertEqual((entry['w'], entry['h']), (64, 48))
        request = json.loads(self.api.requests[0]['body'])
        self.assertTrue(self.api.requests[0]['path'].endswith('/images/generations'))
        self.assertEqual((request['size'], request['model'], request['quality']), ('1024x1536', 'gpt-image-1', 'high'))
        self.assertEqual(self.api.requests[0]['auth'], 'Bearer ' + KEY)
        refs = [self.canvas.add_bytes(self.pid, png('green'))['file'], self.canvas.add_bytes(self.pid, png('white'))['file']]
        self.api.requests.clear()
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '换成胡桃木', 'refs': refs, 'ratio': '1:1'})['id'])
        self.assertEqual(record['state'], 'done', record['error'])
        [request] = self.api.requests
        self.assertTrue(request['path'].endswith('/images/edits'))
        self.assertIn('multipart/form-data', request['type'])
        self.assertEqual(request['body'].count(b'filename='), 2)

    def test_chat_api_image_and_failures(self):
        self.settings.save({'base_url': self.api.url, 'api_key': KEY, 'model': 'gemini-2.5-flash-image'})
        ref = self.canvas.add_bytes(self.pid, png('green'))['file']
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '白底产品图', 'refs': [ref], 'ratio': '16:9'})['id'])
        self.assertEqual(record['state'], 'done', record['error'])
        payload = json.loads(self.api.requests[0]['body'])
        self.assertTrue(self.api.requests[0]['path'].endswith('/chat/completions'))
        self.assertEqual([p['type'] for p in payload['messages'][0]['content']], ['text', 'image_url'])
        self.assertIn('16:9', payload['messages'][0]['content'][0]['text'])
        self.assertIn('2K', payload['messages'][0]['content'][0]['text'])
        self.api.status = 401
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '再来一张'})['id'])
        self.assertEqual(record['state'], 'failed')
        self.assertIn('API Key', record['error'])
        self.assertNotIn(KEY, record['error'])
        self.settings.save({'model': ''})
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '没模型'})['id'])
        self.assertIn('选择模型', record['error'])
        with self.assertRaises(CanvasError):
            self.canvas.generate({'project': self.pid, 'prompt': '  '})

    def test_images_api_relay_formats_and_reasons(self):
        self.settings.save({'base_url': self.api.url, 'api_key': KEY, 'model': 'seedream-5.5'})
        image = base64.b64encode(png('blue')).decode()

        def run(reply):
            self.api.images_reply = reply
            return self.wait(self.canvas.generate({'project': self.pid, 'prompt': '一只小猫', 'ratio': '3:4'})['id'])
        # 认得的写法：data 套一层、b64 字段、图片链接、data URL
        for reply in ({'code': 0, 'data': {'data': [{'b64': image}]}}, {'data': [{'url': self.api.url + '/img.png'}]},
                      {'images': [{'image_url': {'url': 'data:image/png;base64,' + image}}]}):
            with self.subTest(reply=str(reply)[:40]):
                record = run(reply)
                self.assertEqual(record['state'], 'done', record['error'])
        first = json.loads(self.api.requests[0]['body'])
        # 自动：非 gpt-image 的模型像素尺寸、aspect_ratio、ratio、resolution 都带上（同一个比例）
        self.assertEqual((first['size'], first['aspect_ratio'], first['ratio'], first['resolution'], first['n']),
                         ('1536x2048', '3:4', '3:4', '2K', 1))
        self.assertTrue(self.api.requests[0]['idempotency'].startswith('panda-'))
        # 失败原因要带出来，不能只说「没有返回图片」，也不能带出 Key
        record = run({'data': [{'error': {'code': 'InvalidParameter', 'message': 'image size must be at least 3686400 pixels'}}]})
        self.assertIn('at least 3686400', record['error'])
        record = run({'task_id': 't-1', 'status': 'queued'})          # 异步任务：会去查，查不到就说清楚
        self.assertIn('查询生图任务失败', record['error'])
        record = run({'code': 0, 'msg': KEY, 'data': None})
        self.assertIn('接口返回', record['error'])
        self.assertIn('"code": 0', record['error'])
        self.assertNotIn(KEY, record['error'])

    def test_images_api_async_task_is_polled(self):
        self.settings.save({'base_url': self.api.url, 'api_key': KEY, 'model': 'seedream-5.5'})

        def task(task_id, status, **extra):
            return {'object': 'image.task', 'id': task_id, 'task_id': task_id, 'status': status, 'assets': [], 'error': '',
                    'poll_after_ms': 1, 'poll_url': f'/v1/images/tasks/{task_id}', **extra}

        def run(task_id, replies, first=None):
            self.api.images_reply = first or task(task_id, 'queued')
            self.api.tasks[task_id] = replies
            return self.wait(self.canvas.generate({'project': self.pid, 'prompt': '一只小猫', 'ratio': '3:4'})['id'])
        # 提交 → 排队 → 处理中 → 出图（图在 assets 里，相对地址）
        record = run('t1', [task('t1', 'running'), task('t1', 'succeeded', assets=[{'type': 'image', 'url': '/v1/img.png'}])])
        self.assertEqual(record['state'], 'done', record['error'])
        self.assertEqual((record['files'][0]['w'], record['files'][0]['h']), (64, 48))
        polls = [g for g in self.api.gets if '/images/tasks/t1' in g['path']]
        self.assertGreaterEqual(len(polls), 2)
        self.assertEqual(polls[0]['auth'], 'Bearer ' + KEY)
        # 中转站说结果未确认：带出原因和任务号，不重试
        record = run('t2', [task('t2', 'uncertain', error='生成服务报告暂时不可用', error_code='image_result_uncertain',
                                 billed_amount=0, charged_amount=0)])
        self.assertEqual(record['state'], 'failed')
        self.assertIn('生成服务暂时不可用，这次没出图（没扣费）', record['error'])
        self.assertIn('t2', record['error'])
        record = run('t5', [task('t5', 'uncertain', error='上游超时', billed_amount=0.04, charged_amount=0.04)])
        self.assertIn('还没确认', record['error'])
        self.assertNotIn('没扣费', record['error'])
        record = run('t3', [task('t3', 'failed', error='内容违规')])
        self.assertIn('内容违规', record['error'])
        self.assertIn('t3', record['error'])
        # 查询地址在别的站点：不去查，也不把 Key 发过去
        before = len(self.api.gets)
        record = run('t4', [], first=task('t4', 'queued', poll_url='http://example.invalid/v1/images/tasks/t4'))
        self.assertIn('不在生图接口的站点上', record['error'])
        self.assertEqual(len(self.api.gets), before)

    def test_images_api_refused_falls_back_to_chat(self):
        # 渠道设成图片接口，但中转站说这个模型不支持图片接口：自动改走对话接口
        self.settings.save({'base_url': self.api.url, 'api_key': KEY, 'model': 'gemini-nano-banana-2.1', 'mode': 'images'})
        self.api.images_refused = True
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '一只小猫', 'ratio': '3:4'})['id'])
        self.assertEqual(record['state'], 'done', record['error'])
        paths = [r['path'] for r in self.api.requests]
        self.assertTrue(paths[0].endswith('/v1/images/generations'))
        self.assertEqual(paths[1], '/v1beta/models/gemini-nano-banana-2.1:generateContent')   # Gemini 类改走原生接口

    def test_gemini_native_and_seedream_refs(self):
        # Gemini 原生：/v1beta/models/{模型}:generateContent，画幅和分辨率在 imageConfig，参考图是 inlineData 裸 base64
        self.settings.save({'base_url': self.api.url, 'api_key': KEY, 'model': 'gemini-3.1-flash-image-preview', 'mode': 'gemini'})
        ref = self.canvas.add_bytes(self.pid, png('green'))['file']
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '一只小猫', 'ratio': '16:9', 'resolution': '4K',
                                                 'refs': [ref]})['id'])
        self.assertEqual(record['state'], 'done', record['error'])
        [request] = self.api.requests
        self.assertEqual(request['path'], '/v1beta/models/gemini-3.1-flash-image-preview:generateContent')
        self.assertEqual(request['auth'], 'Bearer ' + KEY)
        body = json.loads(request['body'])
        self.assertEqual(body['generationConfig']['imageConfig'], {'aspectRatio': '16:9', 'imageSize': '4K'})
        self.assertEqual(body['generationConfig']['responseModalities'], ['TEXT', 'IMAGE'])
        parts = body['contents'][0]['parts']
        self.assertEqual(parts[0], {'text': '一只小猫'})
        self.assertEqual(parts[1]['inlineData']['mimeType'], 'image/png')
        self.assertFalse(parts[1]['inlineData']['data'].startswith('data:'))
        # 即梦带参考图：multipart，字段 image[]，带档位和画幅
        self.api.requests.clear()
        self.settings.save({'base_url': self.api.url, 'api_key': KEY, 'model': 'seedream-5.5', 'mode': 'images'})
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '换成胡桃木', 'ratio': '4:3', 'refs': [ref]})['id'])
        self.assertEqual(record['state'], 'done', record['error'])
        [request] = self.api.requests
        self.assertTrue(request['path'].endswith('/images/edits'))
        self.assertIn(b'name="image[]"', request['body'])
        for field in (b'name="aspect_ratio"', b'name="ratio"', b'name="resolution"', b'name="size"'):
            self.assertIn(field, request['body'])

    def test_async_success_reads_result_url_when_no_assets(self):
        # 文档写法：查 status_url（summary），success 后没有 assets 就读 result_url 的原结果
        self.settings.save({'base_url': self.api.url, 'api_key': KEY, 'model': 'seedream-5.5'})
        image = base64.b64encode(png('blue')).decode()
        self.api.images_reply = {'object': 'image.task', 'task_id': 'r1', 'status': 'queued', 'poll_after_ms': 1, 'assets': [],
                                 'status_url': '/v1/images/tasks/r1?view=summary', 'result_url': '/v1/images/tasks/full'}
        self.api.tasks['r1'] = [{'task_id': 'r1', 'status': 'dispatching', 'assets': []},
                                {'task_id': 'r1', 'status': 'success', 'assets': []}]
        self.api.tasks['full'] = [{'created': 1, 'data': [{'b64_json': image}]}]
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '一只小猫'})['id'])
        self.assertEqual(record['state'], 'done', record['error'])
        self.assertTrue(any(g['path'] == '/v1/images/tasks/r1?view=summary' for g in self.api.gets))
        self.assertTrue(any(g['path'] == '/v1/images/tasks/full' for g in self.api.gets))

    def test_channel_size_style_and_extra_params(self):
        # 每个渠道自己选比例怎么传，还能加额外参数（接别家中转站时用）
        saved = self.settings.save({'base_url': self.api.url, 'api_key': KEY, 'model': 'seedream-5.0-pro',
                                    'size_style': 'ratio', 'extra': '{"watermark": false, "async": true}'})
        channel = saved['channels'][0]
        self.assertEqual((channel['size_style'], channel['extra']), ('ratio', {'watermark': False, 'async': True}))
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '一只小猫', 'ratio': '1:1'})['id'])
        self.assertEqual(record['state'], 'done', record['error'])
        body = json.loads(self.api.requests[-1]['body'])
        self.assertEqual({k: body.get(k) for k in ('size', 'ratio', 'aspect_ratio', 'resolution', 'watermark', 'async')},
                         {'size': None, 'ratio': '1:1', 'aspect_ratio': None, 'resolution': '2K', 'watermark': False, 'async': True})
        self.settings.save({'id': channel['id'], 'size_style': 'size', 'extra': ''})
        self.wait(self.canvas.generate({'project': self.pid, 'prompt': '一只小猫', 'ratio': '16:9'})['id'])
        body = json.loads(self.api.requests[-1]['body'])
        self.assertEqual((body.get('size'), body.get('ratio'), body.get('watermark')), ('2048x1152', None, None))
        for bad in ('{"model": "x"}', '[1, 2]', '不是 JSON', {'prompt': 'x'}):
            with self.subTest(bad=bad), self.assertRaises(AIError):
                self.settings.save({'id': channel['id'], 'extra': bad})
        with self.assertRaises(AIError):
            self.settings.save({'id': channel['id'], 'size_style': 'pixels'})
        # Gemini 原生：额外参数逐层并进请求
        self.settings.save({'id': channel['id'], 'model': 'gemini-3-pro-image-preview', 'mode': 'gemini',
                            'extra': {'generationConfig': {'imageConfig': {'personGeneration': 'ALLOW_ALL'}}}})
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '一只小猫', 'ratio': '3:4'})['id'])
        self.assertEqual(record['state'], 'done', record['error'])
        config = json.loads(self.api.requests[-1]['body'])['generationConfig']['imageConfig']
        self.assertEqual(config, {'aspectRatio': '3:4', 'imageSize': '2K', 'personGeneration': 'ALLOW_ALL'})

    def test_ratio_is_in_prompt_and_result_is_cropped(self):
        # 比例写死：提示词里写上比例；出图方向对、差一点的居中裁准；方向反了的不裁，提示一下
        self.settings.save({'base_url': self.api.url, 'api_key': KEY, 'model': 'gpt-image-1'})
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '一只小猫', 'ratio': '1:1'})['id'])
        self.assertEqual(record['state'], 'done', record['error'])
        self.assertTrue(json.loads(self.api.requests[-1]['body'])['prompt'].endswith('画面比例 1:1（方图）。'))
        self.assertEqual((record['files'][0]['w'], record['files'][0]['h']), (48, 48))        # 假接口出 64x48，裁成方图
        self.assertEqual(record['error'], '')
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '一只小猫', 'ratio': '4:3'})['id'])
        self.assertEqual((record['files'][0]['w'], record['files'][0]['h'], record['error']), (64, 48, ''))
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '一只小猫', 'ratio': '9:16'})['id'])
        self.assertEqual(record['state'], 'done')
        self.assertEqual((record['files'][0]['w'], record['files'][0]['h']), (64, 48))
        self.assertIn('横竖方向不一样', record['error'])

    def test_models_list_puts_image_models_first(self):
        self.settings.save({'base_url': self.api.url, 'api_key': KEY})
        models = ImageGenerator(self.settings).models()
        self.assertEqual(models['image_models'], ['gemini-2.5-flash-image', 'gpt-image-1'])
        self.assertEqual(models['models'][-1], 'gpt-6.2-sol')

    def test_projects_canvases_board_and_files(self):
        project = self.canvas.project(self.pid)
        self.assertEqual((project['name'], [c['name'] for c in project['canvases']]), ('我的项目', ['画布 1']))
        cid = project['canvases'][0]['id']
        self.assertEqual(self.canvas.board(self.pid, cid)['nodes'], [])
        board = {'view': {'x': 10, 'y': 20, 'zoom': .8}, 'nodes': [{'id': '1', 'x': 0, 'y': 0}], 'edges': [],
                 'groups': [{'id': 'g1', 'name': '分组 1', 'nodes': ['1']}], 'seq': 1}
        self.canvas.save_board(self.pid, cid, board)
        self.assertEqual(self.canvas.board(self.pid, cid)['groups'][0]['nodes'], ['1'])
        added = self.canvas.add_canvas(self.pid)
        self.assertEqual(added['canvas']['name'], '画布 2')
        self.canvas.rename_canvas(self.pid, added['canvas']['id'], '  客厅场景 / 草稿 ')
        self.assertEqual(self.canvas.project(self.pid)['canvases'][1]['name'], '客厅场景  草稿')
        self.canvas.delete_canvas(self.pid, added['canvas']['id'])
        with self.assertRaises(CanvasError):
            self.canvas.delete_canvas(self.pid, cid)              # 最后一张不能删
        with self.assertRaises(CanvasError):
            self.canvas.board(self.pid, 'ffffffff')
        with self.assertRaises(CanvasError):
            self.canvas.save_board(self.pid, cid, {'nodes': 'x'})
        with self.assertRaises(CanvasError):
            self.canvas.add_bytes(self.pid, b'not an image')
        for bad in ('../board.json', 'x.png', 'a' * 32 + '.exe'):
            with self.subTest(bad=bad), self.assertRaises(CanvasError):
                self.canvas.file_path(self.pid, bad)
        source = self.root / '照片.png'
        source.write_bytes(png('yellow', (30, 20)))
        [entry] = self.canvas.add_paths(self.pid, [str(source), str(self.root / '不存在.png')])
        self.assertEqual((entry['w'], entry['h']), (30, 20))
        self.assertTrue((self.canvas.projects / self.pid / 'images' / entry['file']).is_file())
        listing = self.canvas.list_projects()
        self.assertEqual(listing['projects'][0]['thumbs'], [entry['file']])
        # 新项目、改名、删除（进回收站）、恢复
        other = self.canvas.create_project('  ')
        self.assertEqual(other['name'], '未命名项目')
        self.canvas.rename_project(other['id'], '五斗柜')
        self.assertEqual(self.canvas.project(other['id'])['name'], '五斗柜')
        self.canvas.delete_project(other['id'])
        with self.assertRaises(CanvasError):
            self.canvas.project(other['id'])
        self.assertEqual(self.canvas.restore_project(other['id'])['name'], '五斗柜')
        with self.assertRaises(CanvasError):
            self.canvas.project('../../x')

    def test_thumbnails_are_made_once_and_small_images_stay_original(self):
        big = self.canvas.add_bytes(self.pid, png('teal', (2048, 1536)))
        small = self.canvas.add_bytes(self.pid, png('navy', (300, 200)))
        original = self.canvas.file_path(self.pid, big['file'])
        thumb = self.canvas.thumb_path(self.pid, big['file'], 1280)
        self.assertEqual((thumb.parent.name, thumb.suffix), ('thumbs', '.webp'))
        with Image.open(thumb) as image:
            self.assertEqual(image.size, (1280, 960))
        self.assertLess(thumb.stat().st_size, original.stat().st_size)
        made = thumb.stat().st_mtime_ns
        self.assertEqual(self.canvas.thumb_path(self.pid, big['file'], 1280).stat().st_mtime_ns, made)   # 第二次直接用缓存
        with Image.open(self.canvas.thumb_path(self.pid, big['file'], 120)) as image:   # 小于 256 的都给 256 档
            self.assertEqual(max(image.size), 256)
        self.assertEqual(self.canvas.thumb_path(self.pid, small['file'], 1280), self.canvas.file_path(self.pid, small['file']))
        self.assertEqual(self.canvas.thumb_path(self.pid, big['file'], 'abc'), original)   # 参数不对就给原图
        with self.assertRaises(CanvasError):
            self.canvas.thumb_path(self.pid, '../board.json', 256)
        # 项目卡片的缩略图列表只列原图，不会把 thumbs 里的缓存算进去
        self.assertEqual(self.canvas.list_projects()['projects'][0]['image_count'], 2)

    def test_copy_files_between_projects_keeps_names(self):
        entry = self.canvas.add_bytes(self.pid, png('olive', (40, 30)))
        other = self.canvas.create_project('别的项目')['id']
        self.assertEqual(self.canvas.copy_files(other, self.pid, [entry['file'], entry['file'], 'a' * 32 + '.png']),
                         {'files': [entry['file']]})                    # 重复的只拷一次，原项目里没有的跳过
        self.assertEqual(self.canvas.file_path(other, entry['file']).read_bytes(),
                         self.canvas.file_path(self.pid, entry['file']).read_bytes())
        self.assertEqual(self.canvas.copy_files(other, self.pid, [entry['file']]), {'files': [entry['file']]})  # 已经有了不重拷
        for bad in ([], 'x', ['../project.json']):
            with self.subTest(bad=bad):
                if bad == ['../project.json']:
                    self.assertEqual(self.canvas.copy_files(other, self.pid, bad), {'files': []})
                else:
                    with self.assertRaises(CanvasError):
                        self.canvas.copy_files(other, self.pid, bad)

    def test_prefs_remember_gen_settings_and_presets(self):
        self.assertEqual(self.canvas.prefs()['gen'], {'ratio': '16:9', 'quality': 'standard', 'resolution': '2K', 'count': 1})
        self.canvas.save_prefs({'gen': {'ratio': '3:4', 'quality': 'high', 'resolution': '4K', 'count': 3}})
        self.canvas.save_prefs({'gen': {'ratio': '7:3', 'count': 99}})               # 无效值不改
        self.assertEqual(self.canvas.prefs()['gen'], {'ratio': '3:4', 'quality': 'high', 'resolution': '4K', 'count': 3})
        self.canvas.save_prefs({'presets': ['白底主图，柔和阴影', ' ', '白底主图，柔和阴影', '北欧原木风']})
        self.assertEqual(self.canvas.prefs()['presets'], ['白底主图，柔和阴影', '北欧原木风'])
        cid = self.canvas.project(self.pid)['canvases'][0]['id']
        self.canvas.save_prefs({'last': {'project': self.pid, 'canvas': cid}})
        self.assertEqual(self.canvas.prefs()['last'], {'project': self.pid, 'canvas': cid})

    def test_old_single_board_is_migrated(self):
        root = test_directory(self, 'canvas-old-')
        studio = Studio(root, read_json(ROOT / 'config.json'))
        self.addCleanup(studio.calibration.close)
        old = studio.exchange / 'canvas'
        (old / 'files').mkdir(parents=True)
        name = 'b' * 32 + '.png'
        (old / 'files' / name).write_bytes(png())
        (old / 'board.json').write_text(json.dumps({'nodes': [{'id': '1', 'results': [{'file': name}]}], 'edges': []}), encoding='utf-8')
        store = CanvasStore(studio, self.settings)
        [project] = store.list_projects()['projects']
        cid = project['canvases'][0]['id']
        self.assertEqual(project['name'], '我的项目')
        self.assertEqual(store.board(project['id'], cid)['nodes'][0]['results'][0]['file'], name)
        self.assertTrue(store.file_path(project['id'], name).is_file())
        self.assertFalse((old / 'board.json').exists())
        self.assertEqual(store.prefs()['last'], {'project': project['id'], 'canvas': cid})

    def test_channels_save_rename_copy_switch_delete(self):
        self.assertEqual((self.settings.public()['channels'], self.settings.public()['active']), ([], ''))
        first = self.settings.save({'base_url': self.api.url, 'api_key': KEY, 'model': 'gpt-image-1',
                                    'models': ['gpt-image-1', 'gemini-2.5-flash-image']})
        c1 = first['saved']
        [channel] = first['channels']
        self.assertEqual((channel['name'], first['active'], channel['key_source'], channel['key_tail']), ('渠道 1', c1, 'own', '2468'))
        self.assertEqual(channel['models'], ['gpt-image-1', 'gemini-2.5-flash-image'])
        self.assertNotIn(KEY, json.dumps(first))
        # 改名不动 Key
        self.settings.save({'id': c1, 'name': '  中转站 A  '})
        self.assertEqual(self.settings.public()['channels'][0]['name'], '中转站 A')
        self.assertEqual(self.settings.connection(channel=c1)[1], KEY)
        with self.assertRaises(AIError):
            self.settings.save({'id': c1, 'name': '坏' + chr(10) + '名字'})
        # 复制一份（连 Key），换个模型
        c2 = self.settings.copy(c1)['saved']
        self.settings.save({'id': c2, 'name': '中转站 A · Gemini', 'model': 'gemini-2.5-flash-image'})
        public = self.settings.public()
        self.assertEqual([c['name'] for c in public['channels']], ['中转站 A', '中转站 A · Gemini'])
        self.assertEqual(public['active'], c2)
        self.assertEqual(self.settings.connection(channel=c2)[1:3], (KEY, 'gemini-2.5-flash-image'))
        self.assertEqual(self.settings.connection()[2], 'gemini-2.5-flash-image')        # 不指定就用当前渠道
        # 前两个都改过名，再新建的从「渠道 1」数起
        self.assertEqual(self.settings.save({'model': 'x'})['channels'][-1]['name'], '渠道 1')
        # 设置页里拉模型：带 id 时用那个渠道存的 Key；不带 id 时不拿别的渠道的 Key
        self.assertEqual(self.settings.connection({'id': c1, 'base_url': self.api.url})[1], KEY)
        self.assertEqual(self.settings.connection({'base_url': self.api.url, 'reuse_ai_key': False})[1], '')
        # 生图时按节点选的渠道走
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '白底主图', 'channel': c1})['id'])
        self.assertEqual(record['state'], 'done', record['error'])
        self.assertTrue(self.api.requests[-1]['path'].endswith('/images/generations'))
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '白底主图', 'channel': c2})['id'])
        self.assertEqual(record['state'], 'done', record['error'])
        self.assertTrue(self.api.requests[-1]['path'].endswith('/chat/completions'))
        # 设为当前、删除
        self.assertEqual(self.settings.activate(c1)['active'], c1)
        after = self.settings.delete(c1)
        self.assertNotIn(c1, [c['id'] for c in after['channels']])
        self.assertEqual(after['active'], after['channels'][0]['id'])
        self.assertFalse((self.root / 'keys' / 'image_keys' / f'{c1}.bin').exists())
        record = self.wait(self.canvas.generate({'project': self.pid, 'prompt': '白底主图', 'channel': c1})['id'])
        self.assertEqual(record['state'], 'failed')
        self.assertIn('渠道', record['error'])
        with self.assertRaises(CanvasError):
            self.canvas.generate({'project': self.pid, 'prompt': '白底主图', 'channel': '../x'})

    def test_old_single_setting_becomes_channel_one(self):
        (self.root / 'image_api.json').write_text(json.dumps({'base_url': self.api.url, 'model': 'gpt-image-1',
                                                              'mode': 'images', 'reuse_ai_key': False}), encoding='utf-8')
        old_key = self.root / 'keys' / 'image_key.bin'
        old_key.parent.mkdir(parents=True, exist_ok=True)
        old_key.write_bytes(dpapi(KEY.encode()))
        public = ImageAPISettings(self.root, old_key, self.ai).public()
        [channel] = public['channels']
        self.assertEqual((channel['name'], channel['model'], channel['mode'], channel['key_source']), ('渠道 1', 'gpt-image-1', 'images', 'own'))
        self.assertEqual(public['active'], channel['id'])
        self.assertFalse(old_key.exists())
        self.assertEqual(self.settings.connection()[1], KEY)

    def test_running_runs_are_marked_failed_after_restart(self):
        record = {'id': 'a' * 32, 'state': 'running', 'files': [], 'error': ''}
        (self.canvas.runs / ('a' * 32 + '.json')).write_text(json.dumps(record), encoding='utf-8')
        restarted = CanvasStore(self.studio, self.settings)
        self.assertEqual(restarted.run('a' * 32)['state'], 'failed')

    def test_old_runs_are_cleaned_on_start(self):
        import os
        old, recent = self.canvas.runs / ('b' * 32 + '.json'), self.canvas.runs / ('c' * 32 + '.json')
        for path in (old, recent):
            path.write_text(json.dumps({'id': path.stem, 'state': 'done', 'files': [], 'error': ''}), encoding='utf-8')
        month = time.time() - 31 * 86400
        os.utime(old, (month, month))
        restarted = CanvasStore(self.studio, self.settings)
        with self.assertRaises(CanvasError):
            restarted.run('b' * 32)
        self.assertEqual(restarted.run('c' * 32)['state'], 'done')


class ParsingTests(unittest.TestCase):
    def test_chat_image_formats_and_mode(self):
        data = base64.b64encode(png()).decode()
        url = 'data:image/png;base64,' + data
        self.assertEqual(extract_chat_image({'choices': [{'message': {'images': [{'image_url': {'url': url}}]}}]}), png())
        self.assertEqual(extract_chat_image({'choices': [{'message': {'content': [
            {'type': 'text', 'text': 'ok'}, {'type': 'image_url', 'image_url': {'url': url}}]}}]}), png())
        with self.assertRaises(Exception):
            extract_chat_image({'choices': [{'message': {'content': '我不会画图'}}]})
        self.assertEqual(resolve_mode('auto', 'gpt-image-1'), 'images')
        self.assertEqual(resolve_mode('auto', 'gemini-2.5-flash-image'), 'chat')
        self.assertEqual(resolve_mode('chat', 'gpt-image-1'), 'chat')

    def test_sizes_and_quality(self):
        self.assertEqual(size_params('seedream-5.0-pro', '3:4', '2K'),
                         ('1536x2048', {'aspect_ratio': '3:4', 'ratio': '3:4', 'resolution': '2K'}))
        self.assertEqual(size_params('gpt-image-1', '3:4', '4K'), ('1024x1536', {}))
        self.assertEqual(size_params('seedream-5.5', '16:9', '1K', 'aspect_ratio'), (None, {'aspect_ratio': '16:9', 'resolution': '1K'}))
        self.assertEqual(size_params('seedream-5.0-pro', '1:1', '2K', 'ratio'), (None, {'ratio': '1:1', 'resolution': '2K'}))
        self.assertEqual(size_params('flux', '16:9', '2K', 'size'), ('2048x1152', {}))
        self.assertEqual(api_root('https://api.ai-media.vip/v1/'), 'https://api.ai-media.vip')
        self.assertEqual(resolve_mode('gemini', 'gpt-image-1'), 'gemini')
        self.assertEqual(image_size('gpt-image-1', '2:3', '4K'), '1024x1536')      # gpt-image 只认固定尺寸
        self.assertEqual(image_size('flux-1.1-pro', '16:9', '2K'), '2048x1152')
        self.assertEqual(image_size('flux-1.1-pro', '3:4', '4K'), '3072x4096')
        self.assertEqual(image_size('flux', '1:1', '1K'), '1024x1024')
        self.assertEqual(quality_param('gpt-image-1', 'high'), 'high')
        self.assertEqual(quality_param('dall-e-3', 'high'), 'hd')
        self.assertIsNone(quality_param('seedream-4', 'high'))
        self.assertIsNone(quality_param('gpt-image-1', 'standard'))


if __name__ == '__main__':
    unittest.main()
