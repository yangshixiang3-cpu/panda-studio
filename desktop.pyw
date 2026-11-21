"""Panda Studio（原名白底工作台）桌面入口：复用标准库服务，使用 Edge WebView2。"""
import ctypes
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from server.app import LocalServer, handler_for
from server.core import ROOT, Studio
from server.paths import plan_inputs
from server.settings import ensure_max_exe, refresh_max_plugin

ADDRESS = ('127.0.0.1', 8770)
URL = 'http://127.0.0.1:8770/'


def is_studio_service(url=URL):
    try:
        with urllib.request.urlopen(url + 'api/health', timeout=1) as response:
            data = json.loads(response.read(8192))
        return isinstance(data, dict) and data.get('service') == 'whitebg-studio' and data.get('api_version') == 1
    except (OSError, ValueError, urllib.error.URLError):
        return False


def start_service(studio=None, address=ADDRESS):
    """返回本进程拥有的服务与线程；复用时两者都为 None。"""
    try:
        server = LocalServer(address, handler_for(studio or Studio(ROOT)))
    except OSError:
        url = f'http://{address[0]}:{address[1]}/'
        # 另一个桌面进程可能刚绑定端口，给其后台线程短暂启动时间。
        for _ in range(4):
            if is_studio_service(url):
                return None, None
            time.sleep(0.1)
        raise RuntimeError(f'本机端口 {address[1]} 被别的程序占用，无法打开 Panda Studio。请关闭占用程序后重试。')
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
    thread.start()
    return server, thread


def stop_service(server, thread):
    if server is not None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class DesktopBridge:
    def __init__(self):
        self._window = None

    def _plan(self, paths):
        plan = plan_inputs([{'path': str(p) if p and Path(p).exists() else '',
                             'is_directory': bool(p and Path(p).is_dir())} for p in paths])
        # 原样带上拖入的文件夹：拖到「保存到」区域时，用的是文件夹本身而不是它所属的产品。
        plan['folders'] = [str(p) for p in paths if p and Path(p).is_dir()]
        return plan

    def choose_folder(self):
        import webview
        paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return self._plan(paths) if paths else None

    def choose_model(self):
        import webview
        paths = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
                                               file_types=('Max 模型 (*.max)',))
        return self._plan(paths) if paths else None

    def choose_path(self, kind):
        """本机设置用：kind 为 exe（3dsmax.exe）、max（白底模板）或 folder。返回完整路径或 None。"""
        import webview
        if kind == 'folder':
            paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        else:
            types = {'exe': ('3ds Max 程序 (*.exe)',), 'max': ('Max 文件 (*.max)',)}.get(kind)
            if types is None:
                return None
            paths = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False, file_types=types)
        return str(paths[0]) if paths else None

    def _drop(self, event):
        files = event.get('dataTransfer', {}).get('files', [])
        if not files:
            return  # 页面内拖动清单不属于文件拖入。
        plan = self._plan([f.get('pywebviewFullPath') for f in files])
        self._window.evaluate_js('window.whitebgDesktopDrop(' + json.dumps(plan, ensure_ascii=True) + ')')

    # ── 无边框窗口（去掉系统标题栏，页面四周大圆角）：最小化 / 最大化 / 关闭 / 从边缘拉伸都由页面上的按钮调用。
    # 圆角靠把窗口本身裁成圆角矩形（SetWindowRgn）：圆角外的部分窗口不存在，直接透出桌面。
    # （pywebview 的 transparent 在 Windows 上不会让窗体变透明，圆角外会露出窗体底色，所以不用它。）
    _restore_rect = None
    CORNER = 22   # 圆角半径（网页像素），和 style.css 里 html.frameless 的描边圆角一致

    def _apply_region(self, *_):
        from ctypes import wintypes
        hwnd = self._hwnd()
        if not hwnd:
            return
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
        gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
        if self._restore_rect:          # 最大化：直角铺满，不裁
            user32.SetWindowRgn(hwnd, None, True)
            return
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        radius = round(self.CORNER * (user32.GetDpiForWindow(hwnd) or 96) / 96)
        region = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
        if region:
            user32.SetWindowRgn(hwnd, region, True)   # 成功后区域归系统管理，不用再释放

    def _shown(self):
        try:
            self._window.native.SizeChanged += self._apply_region   # 任何方式改了大小（拉伸、贴边）都重新裁
        except Exception:
            pass
        self._apply_region()

    def _hwnd(self):
        native = getattr(self._window, 'native', None)
        return int(native.Handle.ToInt64()) if native is not None else None

    def window_minimize(self):
        self._window.minimize()

    def window_close(self):
        self._window.destroy()

    def window_maximized(self):
        return self._restore_rect is not None

    def window_toggle_maximize(self):
        """最大化按屏幕工作区摆放（无边框窗口用系统最大化会盖住任务栏）；再点一次回到原来的位置和大小。"""
        from ctypes import wintypes
        hwnd = self._hwnd()
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        flags = 0x0004 | 0x0040   # SWP_NOZORDER | SWP_SHOWWINDOW
        if self._restore_rect:
            left, top, right, bottom = self._restore_rect
            self._restore_rect = None
            user32.SetWindowPos(hwnd, 0, left, top, right - left, bottom - top, flags)
            self._apply_region()
            return False

        class MONITORINFO(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                        ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        monitor = user32.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False
        self._restore_rect = (rect.left, rect.top, rect.right, rect.bottom)
        work = info.rcWork
        self._apply_region()                        # 先去掉圆角裁剪再铺满
        user32.SetWindowPos(hwnd, 0, work.left, work.top, work.right - work.left, work.bottom - work.top, flags)
        return True

    def window_resize(self, width, height, edge):
        """从页面边缘拉伸：edge 为 n/s/e/w 的组合，拉哪边就固定对边。宽高是网页像素（逻辑像素）。"""
        from webview.window import FixPoint
        if self._restore_rect:
            return
        edge = str(edge)
        fix = (FixPoint.SOUTH if 'n' in edge else FixPoint.NORTH) | (FixPoint.EAST if 'w' in edge else FixPoint.WEST)
        self._window.resize(max(900, int(width)), max(650, int(height)), fix)

    def _loaded(self):
        from webview.dom import DOMEventHandler
        document = self._window.dom.document
        document.events.drop += DOMEventHandler(self._drop, prevent_default=True, stop_propagation=True)
        document.events.dragover += DOMEventHandler(lambda event: None, prevent_default=True, stop_propagation=True, debounce=100)
        self._window.evaluate_js("window.dispatchEvent(new Event('whitebgdesktopready'))")


def main():
    server = thread = None
    try:
        import webview
        try:
            ensure_max_exe(ROOT)  # 新电脑第一次打开：自动找 3ds Max
            refresh_max_plugin(ROOT)  # 换过安装位置时，把已装插件里的路径改过来
        except Exception:
            pass
        server, thread = start_service()
        bridge = DesktopBridge()
        # 无边框：去掉系统白色标题栏，页面自己画窗口按钮，窗口本身裁成圆角；拖动只认标了 pywebview-drag-region 的元素本身，
        # 点标题栏里的按钮不会带动窗口。
        webview.settings['DRAG_REGION_DIRECT_TARGET_ONLY'] = True
        # shadow=False：pywebview 默认给窗口开系统阴影（把系统边框延伸进窗口），Windows 11 会在窗口背后画一层
        # 8px 小圆角的白边，和我们裁出的 22px 大圆角之间露出白月牙（用户 1.3.0 截图反馈），所以关掉。
        window = webview.create_window('Panda Studio', URL, js_api=bridge, width=1600, height=1000,
                                       resizable=True, min_size=(900, 650), frameless=True,
                                       easy_drag=False, shadow=False, background_color='#ece6df')
        bridge._window = window
        window.events.loaded += bridge._loaded
        window.events.shown += bridge._shown
        webview.start(gui='edgechromium')
        if not window.events.shown.is_set():
            raise RuntimeError('桌面窗口未能启动，请检查本机 Edge WebView2 运行环境。')
        return 0
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(None, '无法打开 Panda Studio：'+str(error), 'Panda Studio', 0x10)
        return 1
    finally:
        stop_service(server, thread)


if __name__ == '__main__':
    raise SystemExit(main())
