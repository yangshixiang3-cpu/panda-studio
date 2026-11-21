"""真实浏览器、临时服务、假执行器和假 Codex 的打光完整验收。"""
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
    run=Path(tempfile.mkdtemp(prefix='lighting-browser-',dir=ROOT/'tests/tmp'))
    shutil.copytree(ROOT/'web',run/'web',ignore=shutil.ignore_patterns('*.bak'))
    product=run/'临时产品';product.mkdir();(product/'柜体.max').write_bytes(b'fake')
    fbx=product/'柜体.fbx';shutil.copy2(ROOT/'tests/fixtures/L型咖啡柜_绑定.fbx',fbx)
    config=read_json(ROOT/'config.json');config.update(exchange_dir='exchange',codex_command=[sys.executable,str(ROOT/'tests/fake_codex.py')],smallshot_size={'portrait':[240,320],'landscape':[320,240]},final_size={'portrait':[240,320],'landscape':[320,240]})
    studio=Studio(run,config);server=LocalServer(('127.0.0.1',0),handler_for(studio))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();stop=threading.Event()
    def execute():
        while not stop.is_set():
            with studio.lock:run_once(studio.exchange,ROOT/'tests/fixtures/L型咖啡柜_绑定.fbx')
            stop.wait(.1)
    runner=threading.Thread(target=execute,daemon=True);runner.start()
    before=snapshot(ROOT/'exchange/jobs')
    report={'run_dir':str(run),'url':f'http://127.0.0.1:{server.server_port}/','errors':[]}
    atomic_json(run/'arguments.json',dict(url=report['url'],product_dir=str(product),fbx=str(fbx),run_dir=str(run),root=str(ROOT)))
    try:
        result=subprocess.run(['node',str(ROOT/'tests/lighting_flow.cjs'),str(run/'arguments.json')],capture_output=True,text=True,encoding='utf-8',timeout=150)
        report.update(browser_exit_code=result.returncode,browser_stdout=result.stdout,browser_stderr=result.stderr)
        browser=read_json(run/'browser-report.json');report['browser']=browser
        if result.returncode:raise AssertionError('浏览器验收未通过，请查看日志。')
        task=studio.load(browser['task_id']);jobs=studio.jobs_for(task['id'])
        renders=[j for j in jobs if j['type']=='render']
        assert renders and all('lighting' in i and 'lighting_text' in i for j in renders for i in j['items'])
        assert all(i['lighting']['azimuth_deg']==-45 for j in renders for i in j['items'])
        report['render_jobs']=[str(studio.exchange/'jobs'/(j['job_id']+'.json')) for j in renders]
        record=read_json(product/'sku/白底清单.json')[0]
        assert record['lighting']['azimuth_deg']==-45 and '左前方' in record['lighting_text']
        diagram=product/'光向参考/1.png';assert diagram.is_file()
        with Image.open(diagram) as im:assert im.width==1200;report['diagram_size']=im.size
        report['lighting_reference']=str(diagram);report['manifest']=str(product/'sku/白底清单.json')
        for folder in (studio.task_dir(task['id'])/'calibration').glob('round-*'):
            prompt=(folder/'prompt.txt').read_text(encoding='utf-8')
            assert '光向和明暗关系以使用者设定为准，不改光向' in prompt
            command=read_json(folder/'command.json');assert str(ROOT/'tests/fake_codex.py') in command
            assert len([v for v in command if v=='-i'])<=12
        screen=ROOT/'tests/screens/06-lighting.png'
        with Image.open(screen) as im:
            im.thumbnail((1000,688));small=ROOT/'tests/screens/06-lighting-small.png';backup(small);im.save(small)
        report['passed']=True
    except Exception as error:report['errors'].append(str(error))
    finally:
        stop.set();runner.join(timeout=10);studio.calibration.close();server.shutdown();server.server_close();thread.join()
        report['formal_jobs_unchanged']=before==snapshot(ROOT/'exchange/jobs')
        if not report['formal_jobs_unchanged']:report['errors'].append('正式任务目录发生变化。')
        atomic_json(ROOT/'tests/lighting-acceptance.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2));return bool(report['errors'])


if __name__=='__main__':raise SystemExit(main())
