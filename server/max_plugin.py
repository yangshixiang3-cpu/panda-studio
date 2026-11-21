"""白底工作台的 Max 插件：一个放在 Max 用户启动脚本目录里的小脚本。

Max 每次启动都会运行 scripts\\startup 里的脚本；插件只做一件事——找到本机工作台的
「启动白底执行器.ms」并运行它，于是 Max 一打开就自动连上工作台。
插件只写在当前 Windows 用户自己的 Max 目录（%LOCALAPPDATA%\\Autodesk\\3dsMax\\<年份> - 64bit\\<语言>\\scripts\\startup），
不碰 Max 安装目录，也不动别的启动脚本。
"""
import os
import re
from pathlib import Path

PLUGIN_NAME = 'WhiteBGStudio_Connect.ms'
MARKER = '-- WhiteBGStudio plugin'


def max_year(exe):
    """从 3dsmax.exe 的路径里认出版本年份（如 2025）；认不出返回 None。"""
    for part in reversed(Path(str(exe or '')).parts):
        match = re.search(r'(20\d\d)', part)
        if match:
            return int(match.group(1))
    return None


def _user_max_dir(year):
    base = os.environ.get('LOCALAPPDATA')
    return Path(base) / 'Autodesk' / '3dsMax' / f'{year} - 64bit' if base and year else None


def startup_dirs(year, create=False):
    """这个版本 Max 的各语言用户启动脚本目录。Max 还没运行过时（create=True）先建中文和英文两个。"""
    base = _user_max_dir(year)
    if base is None:
        return []
    found = []
    if base.is_dir():
        for language in sorted(base.iterdir(), key=lambda p: p.name.casefold()):
            if language.is_dir() and (language / 'scripts').is_dir():
                found.append(language / 'scripts' / 'startup')
    if not found and create:
        found = [base / name / 'scripts' / 'startup' for name in ('CHS', 'ENU')]
    return found


def runner_script(root):
    return (Path(root) / 'max' / '启动白底执行器.ms').resolve()


def plugin_text(root):
    script = str(runner_script(root))
    return (f'{MARKER}（由白底工作台「本机设置」安装，可在那里移除）\n'
            '-- Max 启动时自动连接白底工作台：运行工作台里的「启动白底执行器.ms」。\n'
            '(\n'
            f'    local wbScript = @"{script}"\n'
            '    if doesFileExist wbScript then\n'
            '    (\n'
            '        systemTools.setEnvVariable "WHITEBG_RUNNER_SCRIPT" wbScript\n'
            '        try (fileIn wbScript) catch (format "白底工作台：连接失败 %\\n" (getCurrentException()))\n'
            '    )\n'
            '    else format "白底工作台：找不到 %，请在白底工作台的本机设置里重新安装插件。\\n" wbScript\n'
            ')\n')


def _ours(path):
    try:
        return path.is_file() and path.read_text(encoding='utf-8', errors='ignore').startswith(MARKER)
    except OSError:
        return False


def status(root, exe):
    year = max_year(exe)
    files = [d / PLUGIN_NAME for d in startup_dirs(year)]
    installed = [f for f in files if _ours(f)]
    expected = plugin_text(root)
    current = bool(installed) and all(f.read_text(encoding='utf-8', errors='ignore') == expected for f in installed)
    return {'year': year, 'installed': bool(installed), 'current': current,
            'files': [str(f) for f in installed]}


def install(root, exe):
    year = max_year(exe)
    if year is None:
        raise ValueError('认不出 3ds Max 的版本，请先找到 3dsmax.exe。')
    text = plugin_text(root)
    for folder in startup_dirs(year, create=True):
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / PLUGIN_NAME
        if target.exists() and not _ours(target):
            raise ValueError(f'{target} 已有同名的其他脚本，没有覆盖。')
        target.write_text(text, encoding='utf-8')
    return status(root, exe)


def remove(root, exe):
    for folder in startup_dirs(max_year(exe)):
        target = folder / PLUGIN_NAME
        if _ours(target):
            target.unlink()
    return status(root, exe)


def refresh(root, exe):
    """工作台换了安装位置时，把已装插件里的路径改成当前位置；没装过就什么也不做。"""
    try:
        state = status(root, exe)
        if state['installed'] and not state['current']:
            return install(root, exe)
    except (OSError, ValueError):
        pass
    return None
