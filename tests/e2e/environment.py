import hashlib
import json
import shutil
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
SOURCE = BASE.parents[1]
TMP = SOURCE / 'tests' / 'tmp' / 'e2e'
ROOT = TMP / 'studio'
IGNORED = shutil.ignore_patterns('*.bak', '*.prev', '__pycache__')
PORT = 8796

def setup():
    for name in ('web', 'server', 'max', 'tools', 'refs', 'tests/fixtures'):
        shutil.copytree(SOURCE / name, ROOT / name, ignore=IGNORED, dirs_exist_ok=True)
    if (SOURCE / 'VERSION').is_file():
        shutil.copy2(SOURCE / 'VERSION', ROOT / 'VERSION')
    (ROOT / 'tests' / 'screens').mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE / 'tests' / 'fake_codex.py', ROOT / 'tools' / 'fake_codex.py')
    config = json.loads((SOURCE / 'config.json').read_text(encoding='utf-8-sig'))
    config.update(port=PORT, exchange_dir='exchange', max_exe='', calibration_auto=False,
                  codex_home=None, codex_command=[sys.executable, '-B', str(ROOT / 'tools' / 'fake_codex.py')])
    (ROOT / 'config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    previews = sorted((SOURCE / 'exchange' / 'tasks').glob('*/preview/*.fbx'),
                      key=lambda p: (-p.stat().st_size, str(p)))
    largest = previews[0] if previews else (BASE / '最大预览.fbx')
    if previews:
        shutil.copy2(largest, BASE / '最大预览.fbx')
    print(json.dumps({'file': str(largest), 'bytes': largest.stat().st_size,
                      'sha256': hashlib.sha256(largest.read_bytes()).hexdigest(),
                      'fixture_sha256': hashlib.sha256(next((ROOT / 'tests' / 'fixtures').glob('*.fbx')).read_bytes()).hexdigest()},
                     ensure_ascii=False))

def serve():
    from server.app import LocalServer, handler_for
    from server.core import Studio
    studio = Studio(ROOT)
    with LocalServer(('127.0.0.1', PORT), handler_for(studio)) as server:
        def stop():
            sys.stdin.readline()
            server.shutdown()
        threading.Thread(target=stop, daemon=True).start()
        server.serve_forever(.1)
    studio.calibration.close()

def run_jobs(watch=False):
    from max.wb_core import RunnerHeartbeat
    from tools import fake_runner
    from tools.fake_runner import run_once
    exchange = ROOT / 'exchange'
    fixture = next((ROOT / 'tests' / 'fixtures').glob('*.fbx'))
    heartbeat = RunnerHeartbeat(exchange, 'fake', print)
    heartbeat.start()
    try:
        if watch:
            stopped = threading.Event()
            def monitor():
                for line in sys.stdin:
                    if line.strip() == 'stop':
                        break
                stopped.set()
            threading.Thread(target=monitor, daemon=True).start()
            while not stopped.is_set():
                run_once(exchange, fixture, heartbeat)
                stopped.wait(.2)
            return
        for line in sys.stdin:
            if line.strip() == 'stop':
                break
            if line.strip() == 'once':
                run_once(exchange, fixture, heartbeat)
            if line.strip() == 'partial':
                original = fake_runner.atomic_json
                paused = set()
                def pause(path, value, *args, **kwargs):
                    original(path, value, *args, **kwargs)
                    if path.name.endswith('.status.json') and value.get('state') == 'running':
                        items = value.get('items', [])
                        if sum(item['state'] == 'done' for item in items) == 2 and path not in paused:
                            paused.add(path)
                            time.sleep(5)
                fake_runner.atomic_json = pause
                try:
                    run_once(exchange, fixture, heartbeat)
                finally:
                    fake_runner.atomic_json = original
    finally:
        heartbeat.stop()

def prepare(label, largest):
    product = ROOT / 'products' / label / '测试咖啡柜'
    for name in ('模型', '参考图', 'sku'):
        (product / name).mkdir(parents=True, exist_ok=True)
    (product / '模型' / '开发占位.max').write_text('仅供假执行器测试。', encoding='utf-8')
    fixture = next((ROOT / 'tests' / 'fixtures').glob('*.fbx'))
    fbx = product / '模型' / fixture.name
    shutil.copy2(BASE / '最大预览.fbx' if largest else fixture, fbx)
    is_input=label.endswith('-input')
    reference = product.parent / '同名.png' if is_input else product / '参考图' / '主图.png'
    from PIL import Image
    with Image.open(ROOT / 'tests' / 'fixtures' / '侧面.png') as image:
        image.thumbnail((1200, 1200))
        image.save(reference)
    if label.endswith('-lightbox'):
        shutil.copy2(reference, product / '参考图' / '第二张.png')
    if is_input:
        (product / 'sku' / '旧图.jpg').write_bytes(b'demo')
    print(json.dumps({'product_dir': str(product), 'fbx': str(fbx), 'reference': str(reference)},
                     ensure_ascii=False))

if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    mode, *args = sys.argv[1:]
    if mode == 'setup':
        setup()
    elif mode == 'serve':
        serve()
    elif mode in ('runner', 'watch'):
        run_jobs(mode == 'watch')
    elif mode == 'prepare':
        prepare(args[0], 'largest' in args)
