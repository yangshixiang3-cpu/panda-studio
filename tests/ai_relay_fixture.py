"""本机中转站替身，供接口、校准和浏览器验收共用。"""
import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading

from PIL import Image

from fake_codex import array_overrides


KEY = 'sk-fake-whitebg-3517'
MODEL = 'gpt-6.2-sol'
COLOR = {'description': '砖红色', 'red': 200, 'green': 60, 'blue': 40}
TOKENS = {'prompt_tokens': 12150, 'completion_tokens': 150, 'total_tokens': 12300}


def completion(value, *, fences=False):
    content = json.dumps(value, ensure_ascii=False)
    if fences:
        content = '\n```json\n'+content+'\n```\n'
    return 200, {'choices': [{'message': {'content': content}}], 'usage': TOKENS}


def decode_image(part):
    header, content = part['image_url']['url'].split(',', 1)
    with Image.open(io.BytesIO(base64.b64decode(content))) as image:
        return header, image.copy()


def normal_reply(path, payload):
    if path.endswith('/models'):
        return 200, {'data': [{'id': name} for name in ('z-model', MODEL, 'a-model', MODEL, KEY)]}
    user = payload['messages'][1]['content']
    if user[0]['text'].startswith('本轮数据：'):
        decoder = json.JSONDecoder()
        inputs, _ = decoder.raw_decode(user[0]['text'].split('\n', 1)[1])
        round_no = inputs['round']
        overrides = inputs['applied_overrides']
        if round_no == 0:
            material = inputs['scene_manifest']['materials'][0]['name']
            overrides = {'materials': {material: {'brightness_mul': 1.18}}}
        return completion({'verdict': 'adjust' if round_no == 0 else 'ok',
                           'summary': '建议稍微提亮。' if round_no == 0 else '颜色与参考接近。',
                           'confidence': .92, 'notes': [], 'overrides': array_overrides(overrides)})
    _, image = decode_image(user[1])
    assert image.size == (64, 64) and image.getpixel((32, 32)) == (200, 60, 40)
    return completion(COLOR)


@contextmanager
def serve(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class FakeRelay:
    def __init__(self, reply=normal_reply):
        self.reply = reply
        self.requests = []

    @contextmanager
    def running(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def dispatch(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.command == 'POST' else None
                relay.requests.append((self.path, payload))
                if self.headers.get('Authorization') != 'Bearer '+KEY:
                    status, value = 401, {'error': 'API Key 无效'}
                else:
                    status, value = relay.reply(self.path, payload)
                data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
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
