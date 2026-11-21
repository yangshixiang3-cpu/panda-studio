"""启动真实桌面窗口并截图；任务与交换目录全部隔离在 tests/tmp。"""
import ctypes
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.core import Studio, atomic_json, backup, read_json

loader = importlib.machinery.SourceFileLoader('desktop_smoke_entry', str(ROOT/'desktop.pyw'))
spec = importlib.util.spec_from_loader(loader.name, loader)
desktop = importlib.util.module_from_spec(spec)
loader.exec_module(desktop)


def main():
    import webview
    from PIL import ImageGrab
    run = Path(tempfile.mkdtemp(prefix='desktop-window-',dir=ROOT/'tests'/'tmp'))
    config = read_json(ROOT/'config.json')
    config['exchange_dir'] = str((run/'exchange').relative_to(ROOT))
    studio = Studio(ROOT, config)
    desktop.Studio = lambda root: studio
    report = {'exchange_dir':str(studio.exchange),'gui_opened':False,'screenshot':None,'errors':[]}
    # 无法启动时记录原始错误，避免错误弹窗等候人工点击。
    desktop.ctypes.windll.user32.MessageBoxW = lambda owner,message,title,flags: report['errors'].append(str(message)) or 1
    original_start = webview.start
    original_start_service = desktop.start_service

    def start_isolated_service():
        server, thread = original_start_service()
        if server is None:
            raise RuntimeError('8770 已有工作台服务，隔离桌面验收不能复用它。请先关闭该服务后再验收。')
        return server, thread

    desktop.start_service = start_isolated_service

    def verify():
        window = webview.windows[0]
        try:
            deadline = time.monotonic()+25
            while time.monotonic()<deadline:
                if window.evaluate_js("!!window.__whitebg && !document.getElementById('desktop-buttons').hidden"):
                    break
                time.sleep(.25)
            else:raise RuntimeError('桌面页面未在时限内完成加载。')
            report['gui_opened'] = True
            product = r'C:\Users\<用户名>\Desktop\111(2022)'
            request = urllib.request.Request(desktop.URL+'api/scan',data=json.dumps({'product_dir':product}).encode(),headers={'Content-Type':'application/json'})
            scan = json.load(urllib.request.urlopen(request))
            report['scan'] = scan
            assert any(Path(p).name == '111.max' for p in scan['models'])
            assert scan['sku_exists'] is False
            assert all('maps' not in Path(p).parts for p in scan['models']+scan['reference_images']+scan['sku_files'])
            request = urllib.request.Request(desktop.URL+'api/tasks',data=json.dumps({'product_dir':product}).encode(),headers={'Content-Type':'application/json'})
            task = json.load(urllib.request.urlopen(request))['task']
            report['task_id'] = task['id']
            report['file_status'] = {}
            for path in (r'C:\Windows\win.ini',product+r'\111.jpg',product+r'\111.max'):
                request = urllib.request.Request(desktop.URL+'api/file?'+urllib.parse.urlencode({'path':path,'task_id':task['id']}),method='HEAD')
                try:
                    with urllib.request.urlopen(request) as response:status=response.status
                except urllib.error.HTTPError as error:status=error.code
                report['file_status'][path] = status
            assert list(report['file_status'].values()) == [403,403,200]
            window.evaluate_js("window.whitebgDesktopDrop("+json.dumps({'product':{'product_dir':product,'selected_model':product+r'\111.max'},'images':[],'ignored':0,'missing':0})+")")
            deadline = time.monotonic()+10
            while time.monotonic()<deadline:
                if window.evaluate_js("document.querySelector('input[name=model]:checked')?.value.endsWith('111.max') && document.getElementById('sku-hint').textContent.includes('还没有 sku 文件夹')"):
                    break
                time.sleep(.2)
            else:raise RuntimeError('桌面路径接入或 sku 提示验证失败。')
            time.sleep(.6)
            hwnd = ctypes.windll.user32.FindWindowW(None,'Panda Studio')
            if not hwnd:raise RuntimeError('无法找到真实桌面窗口句柄。')
            screenshot = ROOT/'tests'/'screens'/'03-desktop.png'
            screenshot.parent.mkdir(exist_ok=True)
            backup(screenshot)
            picture = ImageGrab.grab(window=hwnd)
            picture.save(screenshot)
            report['screenshot'] = {'path':str(screenshot),'size':list(picture.size)}
            report['isolated_jobs'] = list(map(str,(studio.exchange/'jobs').iterdir()))
            assert not report['isolated_jobs']
        except Exception as error:
            report['errors'].append(str(error))
        finally:
            window.destroy()

    def start(**kwargs):
        return original_start(func=verify,**kwargs)
    webview.start = start
    code = desktop.main()
    report['exit_code'] = code
    report['owned_service_stopped'] = not desktop.is_studio_service()
    atomic_json(ROOT/'tests'/'desktop-report.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['gui_opened'] and report['screenshot'] and not report['errors'] and report['owned_service_stopped'] else 1


if __name__=='__main__':raise SystemExit(main())
