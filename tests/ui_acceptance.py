"""新增输入流程的隔离验收；不用正式交换目录，不调用 Max。"""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.app import LocalServer, handler_for
from server.core import Studio, atomic_json, read_json
from tools.fake_runner import run_once


def snapshot(folder):
    return {str(p.relative_to(folder)): [p.stat().st_size, p.stat().st_mtime_ns]
            for p in folder.rglob('*') if p.is_file()}


def main():
    run = Path(tempfile.mkdtemp(prefix='input-flow-', dir=ROOT/'tests'/'tmp'))
    product = run/'临时产品'
    (product/'模型'/'日期').mkdir(parents=True)
    (product/'sku').mkdir()
    (product/'sku'/'已有.txt').write_text('只用于隔离验收', encoding='utf-8')
    (product/'根目录.max').write_text('仅用于扫描，不是 Max 模型', encoding='utf-8')
    max_file = product/'模型'/'日期'/'选择.max'
    max_file.write_text('仅用于扫描，不是 Max 模型', encoding='utf-8')
    fbx = product/'模型'/'日期'/'预览.fbx'
    shutil.copy2(ROOT/'tests'/'fixtures'/'L型咖啡柜_绑定.fbx', fbx)
    reference = product/'同名.png'
    shutil.copy2(ROOT/'tests'/'fixtures'/'侧面.png', reference)
    real_product = Path(r'C:\Users\<用户名>\Desktop\111(2022)')
    before_product = snapshot(real_product)
    before_jobs = snapshot(ROOT/'exchange'/'jobs')
    config = read_json(ROOT/'config.json')
    config['exchange_dir'] = str((run/'exchange').relative_to(ROOT))
    studio = Studio(ROOT, config)
    server = LocalServer(('127.0.0.1',0), handler_for(studio))
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    stop = threading.Event()

    def export_previews():
        while not stop.wait(.1):
            with studio.lock:
                run_once(studio.exchange, fbx)

    runner = threading.Thread(target=export_previews, daemon=True)
    runner.start()
    url = f'http://127.0.0.1:{server.server_port}/'
    report = {'url':url,'run_dir':str(run),'exchange_dir':str(studio.exchange),'errors':[]}

    def post(endpoint, value):
        request = urllib.request.Request(url+endpoint,data=json.dumps(value).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request) as response:return json.load(response)

    try:
        scan = post('api/scan',{'product_dir':str(real_product)})
        report['scan'] = scan
        assert any(Path(p).name=='111.max' for p in scan['models'])
        assert scan['sku_exists'] is False
        assert all('maps' not in [part.casefold() for part in Path(p).parts]
                   for key in ('models','reference_images','sku_files') for p in scan[key])
        task = post('api/tasks',{'product_dir':str(real_product)})['task']
        report['readonly_task_id'] = task['id']
        report['file_status'] = {}
        for path in (r'C:\Windows\win.ini',str(real_product/'111.jpg'),str(real_product/'111.max')):
            method = 'HEAD' if path.endswith('.max') else 'GET'
            request = urllib.request.Request(url+'api/file?'+urllib.parse.urlencode({'path':path,'task_id':task['id']}),method=method)
            try:
                with urllib.request.urlopen(request) as response:status=response.status
            except urllib.error.HTTPError as error:status=error.code
            report['file_status'][path]=status
        assert list(report['file_status'].values()) == [403,403,200]
        arguments = {'url':url,'product_dir':str(product),'max_file':str(max_file),'reference':str(reference),
                     'exchange_dir':str(studio.exchange)}
        args_file = run/'browser-arguments.json'
        atomic_json(args_file,arguments)
        result = subprocess.run(['node',str(ROOT/'tests'/'input_flow.cjs'),str(args_file)],capture_output=True,text=True,encoding='utf-8',timeout=100)
        report['browser_exit_code']=result.returncode
        report['browser_stdout']=result.stdout
        report['browser_stderr']=result.stderr
        if result.returncode:report['errors'].append('浏览器输入流程验收失败，详见浏览器输出。')
        report['formal_jobs_unchanged']=snapshot(ROOT/'exchange'/'jobs')==before_jobs
        report['real_product_unchanged']=snapshot(real_product)==before_product
        assert report['formal_jobs_unchanged'] and report['real_product_unchanged']
    except Exception as error:
        report['errors'].append(str(error))
    finally:
        stop.set();runner.join()
        server.shutdown();server.server_close();thread.join()
        report['service_stopped']=not thread.is_alive()
        atomic_json(ROOT/'tests'/'input-acceptance.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return bool(report['errors'])


if __name__=='__main__':raise SystemExit(main())
