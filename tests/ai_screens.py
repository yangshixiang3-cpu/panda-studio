"""在 8796 验证 API 中转站界面并保存截图。"""
import json
import os
import shutil
import subprocess
import threading

from test_ai_api import ROOT, prepare_studio, temp_root
from ai_relay_fixture import FakeRelay, KEY, serve
from server.app import LocalServer, handler_for
from max.wb_core import RunnerHeartbeat
from tools.fake_runner import run_once


def main():
    root = temp_root()
    studio, product, fixture = prepare_studio(root)
    shutil.copytree(ROOT/'web', root/'web')
    shots = ROOT/'tests'/'tmp'/'ai-shots'
    shots.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    heartbeat = RunnerHeartbeat(studio.exchange, 'fake', print)

    def execute():
        while not stop.is_set():
            run_once(studio.exchange, fixture, heartbeat)
            stop.wait(.1)

    with FakeRelay().running() as relay, serve(LocalServer(('127.0.0.1', 8796), handler_for(studio))):
        heartbeat.start()
        runner = threading.Thread(target=execute, daemon=True)
        runner.start()
        try:
            args = {'url': 'http://127.0.0.1:8796/', 'relay': relay.url+'/v1',
                    'product': str(product), 'shots': str(shots)}
            subprocess.run(['node', str(ROOT/'tests'/'ai_screens.cjs'), json.dumps(args)],
                           env={**os.environ, 'WHITEBG_TEST_KEY': KEY}, check=True, timeout=120)
        finally:
            stop.set()
            runner.join()
            heartbeat.stop()
    for file in root.rglob('*'):
        if file.is_file() and file.suffix in ('.json', '.txt', '.log', '.bin'):
            assert KEY.encode() not in file.read_bytes(), str(file)
    print('隔离验收记录位于 '+str(root), flush=True)


if __name__ == '__main__':
    main()
