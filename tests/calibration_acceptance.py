"""隔离服务、假执行器和假 Codex 的实际网页验收；不接触正式作业。"""
import hashlib
import io
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
from server.app import LocalServer, handler_for
from server.core import Studio, atomic_json, read_json, backup
from tools.fake_runner import run_once


def snapshot(folder):
    return {str(p.relative_to(folder)):[p.stat().st_size,p.stat().st_mtime_ns]
            for p in folder.rglob('*') if p.is_file()}


def main():
    run=Path(tempfile.mkdtemp(prefix='calibration-browser-',dir=ROOT/'tests'/'tmp'))
    shutil.copytree(ROOT/'web',run/'web')
    product=run/'临时产品';product.mkdir()
    (product/'模型').mkdir();(product/'模型'/'柜体.max').write_text('开发占位，禁止交给真实 Max',encoding='utf-8')
    fbx=product/'模型'/'柜体.fbx';shutil.copy2(ROOT/'tests'/'fixtures'/'L型咖啡柜_绑定.fbx',fbx)
    (product/'参考图').mkdir();shutil.copy2(ROOT/'tests'/'fixtures'/'侧面.png',product/'参考图'/'主图.png')
    config=read_json(ROOT/'config.json')
    config.update(exchange_dir='exchange',codex_command=[sys.executable,str(ROOT/'tests'/'fake_codex.py')],
                  smallshot_size={'portrait':[240,320],'landscape':[320,240]},
                  final_size={'portrait':[240,320],'landscape':[320,240]})
    studio=Studio(run,config)
    # 参考页交互只改变临时库；真实历史库的认可状态保持原样。
    library=studio.refs.read()
    for index,key in enumerate(library['materials']):
        for n in range(2):
            stream=io.BytesIO()
            image=Image.new('RGB',(320,240),'white')
            from PIL import ImageDraw
            draw=ImageDraw.Draw(image)
            draw.rectangle((60,70,260,190),fill=[(180,140,105),(90,61,44),(235,228,211),(191,167,126),(130,130,130),(45,43,40),(180,200,205)][index])
            draw.line((160,70,160,190),fill='#6A4630',width=2)
            image.save(stream,'PNG')
            studio.refs.add(key,f'开发参考-{key}-{n}.png',stream.getvalue(),library)
    studio.refs.save(library)
    actual_jobs=snapshot(ROOT/'exchange'/'jobs')
    actual_refs=snapshot(ROOT/'refs')
    server=LocalServer(('127.0.0.1',0),handler_for(studio))
    http_thread=threading.Thread(target=server.serve_forever,daemon=True);http_thread.start()
    stop=threading.Event()
    def execute():
        while not stop.is_set():
            run_once(studio.exchange,ROOT/'tests'/'fixtures'/'L型咖啡柜_绑定.fbx')
            stop.wait(.1)
    runner=threading.Thread(target=execute,daemon=True);runner.start()
    report={'run_dir':str(run),'exchange_dir':str(studio.exchange),'errors':[]}
    arguments={'url':f'http://127.0.0.1:{server.server_port}/','product_dir':str(product),'fbx':str(fbx),
               'run_dir':str(run),'root':str(ROOT)}
    atomic_json(run/'arguments.json',arguments)
    try:
        result=subprocess.run(['node',str(ROOT/'tests'/'calibration_flow.cjs'),str(run/'arguments.json')],capture_output=True,text=True,encoding='utf-8',timeout=150)
        report.update(browser_exit_code=result.returncode,browser_stdout=result.stdout,browser_stderr=result.stderr)
        if result.returncode:report['errors'].append('浏览器验收未通过，请查看浏览器日志。')
        browser_report=read_json(run/'browser-report.json')
        report['browser']=browser_report
        task=studio.load(browser_report['task_id'])
        final=next(j for j in reversed(studio.jobs_for(task['id'])) if j.get('mode')=='final')
        report['final_job']=str(studio.exchange/'jobs'/(final['job_id']+'.json'))
        report['final_overrides']=final['overrides']
        report['accepted_round']=task['ai_calibration']['accepted_round']
        report['rounds']=[{'n':r['n'],'verdict':r['verdict']} for r in task['ai_calibration']['rounds']]
        assert final['overrides'] and final['overrides']==task['ai_calibration']['accepted_overrides']
    except Exception as error:
        report['errors'].append(str(error))
    finally:
        stop.set();runner.join(timeout=10)
        studio.calibration.close();server.shutdown();server.server_close();http_thread.join()
        report['formal_jobs_unchanged']=actual_jobs==snapshot(ROOT/'exchange'/'jobs')
        report['real_library_unchanged']=actual_refs==snapshot(ROOT/'refs')
        if not report['formal_jobs_unchanged']:report['errors'].append('正式任务目录发生了变化。')
        if not report['real_library_unchanged']:report['errors'].append('真实参考库发生了变化。')
        atomic_json(ROOT/'tests'/'calibration-acceptance.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return bool(report['errors'])


if __name__=='__main__':raise SystemExit(main())
