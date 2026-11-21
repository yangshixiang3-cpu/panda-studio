"""执行器心跳及本机 Max 的启动状态。"""
import math
import os
import subprocess
import time

BUSY_GRACE_SEC = 1800


def visible_windows(pids):
    """列出属于这些进程、看得见且有标题的顶层窗口：[(pid, 标题)]。最小化的窗口也算看得见。"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                length = user32.GetWindowTextLengthW(hwnd)
                if length:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buffer, length + 1)
                    found.append((pid.value, buffer.value))
        return True
    try:
        user32.EnumWindows(each, 0)
    except OSError:
        return []
    return found


class RunnerStatus:
    def __init__(self, root, exchange, config):
        self.root = root
        self.heartbeat_path = exchange / 'runner.heartbeat.json'
        self.executable = (root / config['max_exe']).resolve() if config.get('max_exe') else None
        self._max_running = False
        self._max_windows = []
        self._max_hidden = 0
        self._probe_until = 0

    def max_running(self):
        if time.monotonic() >= self._probe_until:
            probe = self._probe_max()
            if isinstance(probe, bool):          # 兼容只返回真假值的旧调用和测试替身
                probe = (probe, [], 0)
            self._max_running, self._max_windows, self._max_hidden = probe
            self._probe_until = time.monotonic() + 5
        return self._max_running

    @staticmethod
    def _probe_max():
        """返回（有没有用户能看到的 Max，可见 Max 的窗口标题，后台隐藏的 Max 个数）。
        只把有可见窗口的 3dsmax.exe 算作「已打开」：建模等自动化会隐藏启动 Max，正在关闭的 Max 也会短暂残留进程，
        这些都不是用户打开的那个 Max，算进去会让状态一直显示「Max 已打开、没连接」。"""
        if os.name != 'nt':
            return False, [], 0
        try:
            result = subprocess.run(
                ['tasklist.exe', '/FI', 'IMAGENAME eq 3dsmax.exe', '/FO', 'CSV', '/NH'],
                capture_output=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return False, [], 0
        pids = set()
        for line in result.stdout.splitlines() if result.returncode == 0 else []:
            fields = line.decode('mbcs', errors='replace').split('","')
            if len(fields) > 1 and fields[0].lower().lstrip('"') == '3dsmax.exe' and fields[1].isdigit():
                pids.add(int(fields[1]))
        if not pids:
            return False, [], 0
        windows = visible_windows(pids)
        titles = [title for pid, title in windows if pid in pids]
        shown = {pid for pid, _ in windows}
        return bool(titles), titles[:3], len(pids - shown)

    def status(self):
        from .core import read_json

        heartbeat = {}
        try:
            document = read_json(self.heartbeat_path)
            if isinstance(document, dict):
                heartbeat = document
        except (OSError, ValueError):
            pass
        epoch = heartbeat.get('epoch')
        age = max(0, time.time() - epoch) if type(epoch) in (float, int) and math.isfinite(epoch) else None
        max_running = self.max_running()
        # Corona 同步渲染会卡住 Max 主线程，期间写不了心跳：最后一次心跳报"忙"且 Max 进程还在时，最长 30 分钟内仍算在线。
        online = (not heartbeat.get('stopped') and age is not None
                  and (age <= 12 or (heartbeat.get('busy') is True and max_running and age <= BUSY_GRACE_SEC)))
        return {'online': online, 'age_sec': age, 'busy': online and heartbeat.get('busy') is True,
                'job_id': heartbeat.get('job_id') if online else None, 'max_running': max_running,
                'max_windows': self._max_windows, 'max_hidden': self._max_hidden,
                'can_start': not online and not max_running and self.executable is not None
                and self.executable.is_file()}

    def start(self):
        from .core import StudioError

        status = self.status()
        if status['online']:
            raise StudioError('执行器已经在运行。', 409)
        if status['max_running']:
            title = status['max_windows'][0] if status['max_windows'] else '3ds Max'
            raise StudioError(f'已经开着一个 Max（{title}），但它没有加载白底执行器。'
                              '把这个 Max 关掉后再点「启动 Max」；如果关掉重开还是连不上，到「本机设置」点「一键查找并安装插件」。', 409)
        if not status['can_start']:
            raise StudioError('找不到 3ds Max：到「本机设置」点「一键查找并安装插件」。')
        script = str((self.root / 'max' / '启动白底执行器.ms').resolve())
        options = ({'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == 'nt' else {'start_new_session': True})
        subprocess.Popen([str(self.executable), '-U', 'MAXScript', script],
                         env={**os.environ, 'WHITEBG_RUNNER_SCRIPT': script},
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         **options)
        self._max_running = True
        self._probe_until = time.monotonic() + 5
        return {'started': True}
