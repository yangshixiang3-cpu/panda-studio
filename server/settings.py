"""本机设置：3ds Max 程序和插件、历史白底目录。

白底模板随程序内置（template/白底模板.max），不再让用户选。
「一键查找并安装」：找到本机 3ds Max，再把连接插件装进 Max 的用户启动脚本目录，之后 Max 一打开就自动连上。
数据目录（exchange）按安全规则固定在工作台文件夹内，只展示不让改。
"""
import os
import subprocess
from pathlib import Path

from . import max_plugin

MAX_EXE_NAME = '3dsmax.exe'


def _file_ok(value, suffix, name=None):
    if not isinstance(value, str) or not value.strip():
        return False
    path = Path(value)
    return (path.is_absolute() and path.is_file() and path.suffix.lower() == suffix
            and (name is None or path.name.lower() == name))


def detect_max():
    """从注册表和常见安装位置找 3dsmax.exe，新版本排前面。"""
    found = []
    if os.name == 'nt':
        import winreg
        for major in range(32, 22, -1):  # 3ds Max 2030 … 2021
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf'SOFTWARE\Autodesk\3dsMax\{major}.0', 0,
                                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                    folder, _ = winreg.QueryValueEx(key, 'Installdir')
            except OSError:
                continue
            exe = Path(folder) / MAX_EXE_NAME
            if exe.is_file():
                found.append(str(exe))
        for base in {os.environ.get('ProgramFiles', r'C:\Program Files'), r'D:\Program Files'}:
            for folder in sorted((Path(base) / 'Autodesk').glob('3ds Max 20*'), reverse=True):
                exe = folder / MAX_EXE_NAME
                if exe.is_file():
                    found.append(str(exe))
    return list(dict.fromkeys(found))


def ensure_max_exe(root):
    """首次在新电脑上运行：config 里的 3ds Max 路径无效时，自动找一个填进去。返回是否改了。"""
    from .core import atomic_json, read_json

    path = Path(root) / 'config.json'
    try:
        config = read_json(path)
    except (OSError, ValueError):
        return False
    if _file_ok(config.get('max_exe') or '', '.exe', MAX_EXE_NAME):
        return False
    found = detect_max()
    if not found:
        return False
    config['max_exe'] = found[0]
    atomic_json(path, config)
    return True


def refresh_max_plugin(root):
    """工作台启动时：插件装过但指向旧位置（比如换了安装目录）就顺手改过来。"""
    from .core import read_json

    try:
        exe = read_json(Path(root) / 'config.json').get('max_exe') or ''
    except (OSError, ValueError):
        return None
    return max_plugin.refresh(root, exe) if _file_ok(exe, '.exe', MAX_EXE_NAME) else None


class MachineSettings:
    def __init__(self, studio):
        self.studio = studio

    def read(self):
        config = self.studio.config
        max_exe = config.get('max_exe') or ''
        roots = [str(p) for p in config.get('history_roots') or [] if isinstance(p, str)]
        checks = {'max_exe': _file_ok(max_exe, '.exe', MAX_EXE_NAME),
                  'template': self.studio.template_path().is_file(),
                  'history_roots': [Path(p).is_dir() for p in roots]}
        try:
            plugin = max_plugin.status(self.studio.root, max_exe) if checks['max_exe'] else None
        except OSError:
            plugin = None
        return {'max_exe': max_exe, 'max_year': max_plugin.max_year(max_exe) if checks['max_exe'] else None,
                'plugin': plugin, 'history_roots': roots,
                'data_dir': str(self.studio.exchange), 'app_dir': str(self.studio.root),
                'checks': checks, 'render_target': self.studio.remote.target(),
                # 用内网渲染机出图时，本机不需要装 Max、也不需要模板
                'ready': bool(self.studio.remote.target()) or (checks['max_exe'] and checks['template'])}

    def connect_max(self):
        """一键：没有有效的 3dsmax.exe 就自动找一个；再装好（或更新）连接插件。"""
        from .core import StudioError

        max_exe = self.studio.config.get('max_exe') or ''
        if not _file_ok(max_exe, '.exe', MAX_EXE_NAME):
            found = detect_max()
            if not found:
                raise StudioError('这台电脑上没找到 3ds Max。装好 Max 后再点一次，或在下面手动指定 3dsmax.exe。', 404)
            self.save({'max_exe': found[0]})
            max_exe = found[0]
        try:
            max_plugin.install(self.studio.root, max_exe)
        except (OSError, ValueError) as error:
            raise StudioError('插件没装上：' + str(error))
        data = self.read()
        running = self.studio.runner.status()
        data['message'] = ('已装好。Max 正开着：关掉重开一次就会自动连上。' if running['max_running'] and not running['online']
                           else '已装好。以后 Max 一打开就会自动连上工作台。')
        return data

    def remove_plugin(self):
        max_exe = self.studio.config.get('max_exe') or ''
        if _file_ok(max_exe, '.exe', MAX_EXE_NAME):
            max_plugin.remove(self.studio.root, max_exe)
        return self.read()

    def save(self, values):
        from .core import StudioError, atomic_json, read_json

        if not isinstance(values, dict):
            raise StudioError('设置格式无效。')
        changes = {}
        if 'max_exe' in values:
            value = str(values['max_exe'] or '').strip().strip('"')
            if value and not _file_ok(value, '.exe', MAX_EXE_NAME):
                raise StudioError('请选择 3ds Max 安装目录里的 3dsmax.exe。')
            changes['max_exe'] = value
        if 'history_roots' in values:
            roots = values['history_roots']
            if not isinstance(roots, list) or len(roots) > 50:
                raise StudioError('历史白底目录最多 50 个。')
            cleaned = []
            for root in roots:
                text = str(root or '').strip().strip('"')
                if not text:
                    continue
                if not Path(text).is_absolute() or not Path(text).is_dir():
                    raise StudioError('找不到这个文件夹：' + text)
                cleaned.append(text)
            changes['history_roots'] = list(dict.fromkeys(cleaned))
        if changes:
            path = self.studio.root / 'config.json'
            config = read_json(path)
            config.update(changes)
            atomic_json(path, config)
            self.studio.config.update(changes)
            if 'max_exe' in changes:
                runner = self.studio.runner
                runner.executable = Path(changes['max_exe']).resolve() if changes['max_exe'] else None
                runner._probe_until = 0
        return self.read()

    def open_data_dir(self):
        if os.name == 'nt':
            os.startfile(str(self.studio.exchange))  # noqa: S606 只打开工作台自己的数据目录
        else:
            subprocess.Popen(['xdg-open', str(self.studio.exchange)])
        return {'opened': True}
