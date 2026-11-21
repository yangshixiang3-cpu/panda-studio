"""隔离交换目录和随机端口的灯箱真实浏览器验收。"""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from server.app import LocalServer,handler_for
from server.core import Studio,atomic_json,read_json,backup
from tools.fake_runner import run_once

def snapshot(folder):
    return {str(p.relative_to(folder)):[p.stat().st_size,p.stat().st_mtime_ns] for p in folder.rglob('*') if p.is_file()}

def main():
    run=Path(tempfile.mkdtemp(prefix='lightbox-browser-',dir=ROOT/'tests/tmp'))
    shutil.copytree(ROOT/'web',run/'web',ignore=shutil.ignore_patterns('*.bak'))
    product=run/'临时产品';product.mkdir();(product/'柜体.max').write_bytes(b'fake')
    fbx=product/'柜体.fbx';shutil.copy2(ROOT/'tests/fixtures/L型咖啡柜_绑定.fbx',fbx)
    refs=product/'参考图';refs.mkdir()
    for n in range(2):shutil.copy2(ROOT/'tests/fixtures/侧面.png',refs/f'参考图{n+1}.png')
    config=read_json(ROOT/'config.json');config.update(exchange_dir='exchange',codex_command=[sys.executable,str(ROOT/'tests/fake_codex.py')],smallshot_size={'portrait':[360,480],'landscape':[480,360]})
    studio=Studio(run,config)
    # 参考库仍用正式接口读取，素材仅加入临时工作台。
    library=studio.refs.read()
    for n in range(2):studio.refs.add('摩卡红橡',str(refs/f'参考图{n+1}.png'),library=library)
    studio.refs.save(library)
    server=LocalServer(('127.0.0.1',0),handler_for(studio));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();stop=threading.Event()
    def execute():
        while not stop.is_set():
            with studio.lock:run_once(studio.exchange,ROOT/'tests/fixtures/L型咖啡柜_绑定.fbx')
            stop.wait(.1)
    runner=threading.Thread(target=execute,daemon=True);runner.start();before=snapshot(ROOT/'exchange/jobs')
    args=dict(url=f'http://127.0.0.1:{server.server_port}/',product_dir=str(product),fbx=str(fbx),run_dir=str(run),root=str(ROOT));atomic_json(run/'arguments.json',args)
    report={'临时目录':str(run),'测试地址':args['url']}
    try:
        result=subprocess.run(['node',str(ROOT/'tests/lightbox_flow.cjs'),str(run/'arguments.json')],capture_output=True,text=True,encoding='utf-8',timeout=180)
        report.update(退出码=result.returncode,输出=result.stdout,错误=result.stderr)
        report['浏览器']=read_json(run/'browser-report.json')
        if result.returncode:raise AssertionError('浏览器验收失败')
        with Image.open(ROOT/'tests/screens/07-lightbox.png') as im:
            im.thumbnail((960,660));small=ROOT/'tests/screens/07-lightbox-small.png';backup(small);im.save(small)
        report['通过']=True
    except Exception as error:report['未完成原因']=str(error)
    finally:
        stop.set();runner.join(timeout=10);studio.calibration.close();server.shutdown();server.server_close();thread.join()
        report['正式队列未变化']=before==snapshot(ROOT/'exchange/jobs');atomic_json(ROOT/'tests/lightbox-acceptance.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2));return not report.get('通过',False)

if __name__=='__main__':raise SystemExit(main())
