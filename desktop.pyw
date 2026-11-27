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
WEBVIEW_DATA_DIR = 'webview-data'


def webview_storage_dir(root=ROOT):
    """WebView2 用户数据与缓存目录：留在程序目录（D 盘），不写到 C 盘 AppData/Temp。"""
    return Path(root) / WEBVIEW_DATA_DIR


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
        server = LocalServer(address, handler_for(studio or Studio(ROOT, defer_startup=True)))
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
        self._close_ready = False

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
    # 分屏（1.6.2）：pywebview 自带的拖动是网页里一点点挪窗口，不走系统的「移动窗口」，拖到屏幕边上不会分屏；
    # 无边框窗体也缺系统窗口的样式，Win+方向键、贴边分屏都用不了。所以：
    #   1. 给窗口补上标题栏、可拉伸、最大化 / 最小化这些样式（_frame），再在 WM_NCCALCSIZE 里把系统边框和标题栏藏掉；
    #   2. 拖动标题区时交给系统移动窗口（window_start_drag），这样贴边分屏、Win11 拖到顶部的布局、拖动最大化窗口自动还原都有；
    #   3. 最大化用系统的最大化（ShowWindow），和分屏、Win+↑↓ 是同一套状态。
    _subclass_proc = None
    CORNER = 22   # 圆角半径（网页像素），和 style.css 里 html.frameless 的描边圆角一致
    FRAME_STYLES = 0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000   # 标题栏、可拉伸、系统菜单、最小化、最大化

    def _apply_region(self, *_):
        from ctypes import wintypes
        hwnd = self._hwnd()
        if not hwnd:
            return
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
        gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
        if user32.IsZoomed(hwnd):       # 最大化：直角铺满，不裁
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
            self._window.native.SizeChanged += self._on_size   # 任何方式改了大小（拉伸、贴边、分屏）都重新裁
        except Exception:
            pass
        self._on_ui(self._frame)
        self._apply_region()

    def _on_ui(self, func, wait=True):
        """在窗口自己的界面线程里执行（改窗口样式、子类化、开始系统拖动都要在这个线程里做）。"""
        native = getattr(self._window, 'native', None)
        if native is None:
            return
        from System import Action
        if wait:
            native.Invoke(Action(func))
        else:
            native.BeginInvoke(Action(func))

    def _on_size(self, *_):
        self._ensure_styles()
        self._apply_region()

    def _ensure_styles(self):
        """WinForms 有时会按自己的设置重算窗口样式，把补上的样式冲掉；改大小时检查一下，少了就补回来。"""
        from ctypes import wintypes
        hwnd = self._hwnd()
        if not hwnd or self._subclass_proc is None:
            return
        user32 = ctypes.windll.user32
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        style = user32.GetWindowLongPtrW(hwnd, -16)        # GWL_STYLE
        if style & self.FRAME_STYLES == self.FRAME_STYLES:
            return
        user32.SetWindowLongPtrW(hwnd, -16, style | self.FRAME_STYLES)
        # SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE：让系统按新样式重算一次边框（由 WM_NCCALCSIZE 藏掉）
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0020 | 0x0002 | 0x0001 | 0x0004 | 0x0010)

    def _frame(self):
        """补上系统窗口样式，并子类化窗口：WM_NCCALCSIZE 时让整个窗口都是网页（不画系统边框和标题栏）；
        系统最大化时窗口会比屏幕多出一圈边框，往里收回，内容正好铺满工作区。"""
        from ctypes import wintypes
        hwnd = self._hwnd()
        if not hwnd or self._subclass_proc is not None:
            return
        user32, comctl32 = ctypes.windll.user32, ctypes.windll.comctl32
        lresult = ctypes.c_ssize_t
        subclass_proc = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                           ctypes.c_size_t, ctypes.c_size_t)
        comctl32.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        comctl32.DefSubclassProc.restype = lresult
        comctl32.SetWindowSubclass.argtypes = [wintypes.HWND, subclass_proc, ctypes.c_size_t, ctypes.c_size_t]
        comctl32.SetWindowSubclass.restype = wintypes.BOOL

        class NCCALCSIZE_PARAMS(ctypes.Structure):
            _fields_ = [('rgrc', wintypes.RECT * 3), ('lppos', ctypes.c_void_p)]

        def proc(window, message, wparam, lparam, _id, _data):
            if message == 0x0083 and wparam:            # WM_NCCALCSIZE
                if user32.IsZoomed(window):
                    params = ctypes.cast(lparam, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents
                    dpi = user32.GetDpiForWindow(window) or 96
                    pad = user32.GetSystemMetricsForDpi(92, dpi)           # SM_CXPADDEDBORDER
                    fx = user32.GetSystemMetricsForDpi(32, dpi) + pad      # SM_CXFRAME
                    fy = user32.GetSystemMetricsForDpi(33, dpi) + pad      # SM_CYFRAME
                    rect = params.rgrc[0]
                    rect.left += fx
                    rect.top += fy
                    rect.right -= fx
                    rect.bottom -= fy
                return 0
            return comctl32.DefSubclassProc(window, message, wparam, lparam)

        self._subclass_proc = subclass_proc(proc)          # 一直留着引用，不然回调会被回收
        if comctl32.SetWindowSubclass(hwnd, self._subclass_proc, 1, 0):
            self._ensure_styles()
        else:
            self._subclass_proc = None

    def _hwnd(self):
        native = getattr(self._window, 'native', None)
        return int(native.Handle.ToInt64()) if native is not None else None

    def window_minimize(self):
        self._window.minimize()

    def window_close(self):
        # 网页右上角的关闭按钮：网页已经先把没存的改动存好了，直接关
        self._close_ready = True
        self._window.destroy()

    def _closing(self):
        """Alt+F4、任务栏「关闭窗口」：先不关，请网页把还没存的改动（画布、任务页都是停一下才存）存好后自己关；
        网页 4 秒内没关上（卡住、出错）就直接关。这个事件在界面线程里同步执行，不能在这里等网页，所以放到后台线程。"""
        if self._close_ready or self._window is None:
            return True
        self._close_ready = True
        window = self._window

        def save_then_close():
            try:
                asked = window.evaluate_js('window.__whitebgCloseSoon ? (window.__whitebgCloseSoon(), true) : false')
            except Exception:
                asked = False
            if asked:
                window.events.closed.wait(4)
            if not window.events.closed.is_set():
                try:
                    window.destroy()
                except Exception:
                    pass
        threading.Thread(target=save_then_close, daemon=True).start()
        return False

    def window_maximized(self):
        hwnd = self._hwnd()
        return bool(hwnd and ctypes.windll.user32.IsZoomed(hwnd))

    def window_toggle_maximize(self):
        """系统最大化 / 还原（和贴边分屏、Win+↑↓ 是同一套状态；样式里有标题栏，最大化不会盖住任务栏）。"""
        hwnd = self._hwnd()
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9 if user32.IsZoomed(hwnd) else 3)   # SW_RESTORE / SW_MAXIMIZE
        self._apply_region()
        return bool(user32.IsZoomed(hwnd))

    def window_start_drag(self):
        """按住标题区拖动：交给系统移动窗口（贴边分屏、拖到顶部选布局、拖动最大化窗口自动还原都靠它）。"""
        from ctypes import wintypes
        hwnd = self._hwnd()
        user32 = ctypes.windll.user32
        if not hwnd or not user32.GetAsyncKeyState(0x01) & 0x8000:     # 左键已经松开了就不拖
            return False

        def start():
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.ReleaseCapture()
            # WM_NCLBUTTONDOWN + HTCAPTION：就像按在系统标题栏上，系统接管拖动
            user32.PostMessageW(hwnd, 0x00A1, 2, ((point.y & 0xFFFF) << 16) | (point.x & 0xFFFF))
        self._on_ui(start, wait=False)
        return True

    def window_resize(self, width, height, edge):
        """从页面边缘拉伸：edge 为 n/s/e/w 的组合，拉哪边就固定对边。宽高是网页像素（逻辑像素）。"""
        from webview.window import FixPoint
        hwnd = self._hwnd()
        if hwnd and ctypes.windll.user32.IsZoomed(hwnd):
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
    import sys
    if '--render-node' in sys.argv[1:]:
        # 内网渲染机后台服务（开机自启登记的就是这个）：不开窗口，不占 8770
        from server.render_node import run_forever
        return run_forever(ROOT)
    server = thread = bg_init = None
    try:
        import webview
        from server.node_setup import ensure_service
        root = ROOT
        fn_ensure_max = ensure_max_exe
        fn_refresh_plugin = refresh_max_plugin
        fn_ensure_service = ensure_service
        # 先启动本地 HTTP 服务并创建窗口；查找 Max、刷新插件、拉起渲染机服务放到后台线程执行，不耽误秒开窗口
        server, thread = start_service()

        def _background_desktop_init():
            try:
                found = fn_ensure_max(root)
                if found and server is not None:
                    st = getattr(server.RequestHandlerClass, 'studio_instance', None)
                    if st is not None and st.runner.executable is None:
                        st.config['max_exe'] = str(found)
                        st.runner.executable = Path(found).resolve()
                fn_refresh_plugin(root)
            except Exception:
                pass
            try:
                fn_ensure_service(root)
            except Exception:
                pass

        bg_init = threading.Thread(target=_background_desktop_init, daemon=True, name='desktop-bg-init')
        bg_init.start()
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
        window.events.closing += bridge._closing
        storage = webview_storage_dir(root)
        storage.mkdir(parents=True, exist_ok=True)
        # private_mode=False + storage_path：保留页面 localStorage（上次页面、动效开关），且所有缓存只写到程序目录
        webview.start(gui='edgechromium', private_mode=False, storage_path=str(storage))
        if not window.events.shown.is_set():
            raise RuntimeError('桌面窗口未能启动，请检查本机 Edge WebView2 运行环境。')
        return 0
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(None, '无法打开 Panda Studio：'+str(error), 'Panda Studio', 0x10)
        return 1
    finally:
        stop_service(server, thread)
        if bg_init is not None:
            bg_init.join(timeout=1.0)


if __name__ == '__main__':
    raise SystemExit(main())
