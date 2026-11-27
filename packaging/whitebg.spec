# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 配置：把白底工作台（桌面窗口 + 本地服务 + 中转站 SDK）打成一个文件夹。
# 构建：在 whitebg-studio 目录运行 packaging\build.ps1
# 程序和数据都放在安装目录（contents_directory='.'），所以 server.core.ROOT 就是 exe 所在目录。
import json
import re
import shutil
import sys
from pathlib import Path, PureWindowsPath
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

ROOT = Path(SPECPATH).parent
PYLIBS = ROOT / 'pylibs'
STAGING = ROOT / 'packaging' / 'build' / 'staging'   # 打包前生成的文件（版本资源、去掉本机路径的配置和参考库清单）
# 中转站 SDK（openai 及依赖）装在 pylibs；先加进搜索路径，收集子模块时才找得到
sys.path.insert(0, str(PYLIBS))
sys.path.insert(0, str(ROOT))
SKIP_DIRS = {'__pycache__', 'tests', 'tmp'}
SKIP_SUFFIX = {'.bak', '.prev', '.pyc', '.tmp'}
# 不进安装包：开发用文件，以及会单独生成干净副本的清单
SKIP_FILES = {'web/package.json', 'refs/build-report.json', 'refs/library.json', 'refs/builtin.json'}
# 用不到的第三方文件：安卓、32 位组件和 XML 文档。
# 注意：pywebview/platforms/edgechromium.py 启动时会无条件遍历 win-arm64、win-x64、win-x86 三个 runtimes 目录，
# 缺任一个都会报 FileNotFoundError: Cannot find win-arm64，因此绝不能排除 runtimes/win-*！
SKIP_BINARIES = re.compile(r'(^|/)(pywebview-android\.jar|WebBrowserInterop\.x86\.dll|Python\.Runtime\.xml)$'
                           r'|clr_loader/ffi/dlls/x86/', re.I)

VERSION = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
if not re.fullmatch(r'\d+\.\d+\.\d+', VERSION):
    raise SystemExit(f'VERSION 格式不对：{VERSION!r}')
shutil.rmtree(STAGING, ignore_errors=True)
(STAGING / 'refs').mkdir(parents=True, exist_ok=True)


def tree(folder):
    """把 ROOT/folder 下的文件按原相对路径放到程序目录旁边。"""
    items = []
    for path in sorted((ROOT / folder).rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if SKIP_DIRS & set(rel.parts) or path.suffix.lower() in SKIP_SUFFIX or rel.as_posix() in SKIP_FILES:
            continue
        items.append((str(path), str(rel.parent)))
    return items


def staged(name, data):
    path = STAGING / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    return str(path)


def strip_sources(value):
    """参考库清单里的 source 是建库时的本机原图路径，打包时只留文件名。"""
    if isinstance(value, dict):
        return {key: (PureWindowsPath(item).name if key == 'source' and isinstance(item, str)
                      and PureWindowsPath(item).is_absolute() else strip_sources(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [strip_sources(item) for item in value]
    return value


# 安装包只带不含本机路径的默认配置（config.default.json），装到程序目录里叫 config.json
config = json.loads((ROOT / 'config.default.json').read_text(encoding='utf-8'))
if config.get('max_exe') or config.get('history_roots'):
    raise SystemExit('config.default.json 里不能有本机路径（max_exe、history_roots 要留空）')
datas = tree('web') + tree('max') + tree('refs') + tree('template')
datas += [(staged('config.json', config), '.'), (str(ROOT / 'VERSION'), '.')]
for name in ('library.json', 'builtin.json'):
    source = ROOT / 'refs' / name
    if source.is_file():
        datas.append((staged('refs/' + name, strip_sources(json.loads(source.read_text(encoding='utf-8')))), 'refs'))
# 服务端按 ROOT/server/ 读取的数据文件（如 AI 校准的输出结构 calibration_schema.json）。
# 代码本身打进了 PYZ，这些 .json 不会自动带上；1.1.0 漏了它，AI 校准在安装版里报「找不到 calibration_schema.json」。
datas += [(str(path), 'server') for path in sorted((ROOT / 'server').glob('*.json'))]
if not any(Path(source).name == 'calibration_schema.json' for source, _ in datas):
    raise SystemExit('没有收集到 server/calibration_schema.json')

# exe 的版本信息（文件属性 → 详细信息）
numbers = tuple(int(part) for part in VERSION.split('.')) + (0,)
version_file = STAGING / 'version_info.txt'
version_file.write_text(str(VSVersionInfo(
    ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
    kids=[StringFileInfo([StringTable('080404B0', [
        StringStruct('CompanyName', 'Panda Studio'), StringStruct('FileDescription', 'Panda Studio'),
        StringStruct('FileVersion', VERSION), StringStruct('InternalName', 'PandaStudio'),
        StringStruct('OriginalFilename', 'PandaStudio.exe'), StringStruct('ProductName', 'Panda Studio'),
        StringStruct('ProductVersion', VERSION)])]),
          VarFileInfo([VarStruct('Translation', [0x0804, 1200])])])), encoding='utf-8')

hiddenimports = (collect_submodules('server') + collect_submodules('openai') + collect_submodules('httpx2')
                 + collect_submodules('httpcore2') + collect_submodules('anyio')
                 + ['openai', 'pydantic', 'pydantic_core', 'jiter', 'truststore', 'sniffio', 'h11', 'idna',
                    'webview.platforms.edgechromium', 'webview.platforms.winforms', 'clr_loader'])
if not any(name.startswith('openai.') for name in hiddenimports):
    raise SystemExit('没有收集到 openai SDK，请检查 pylibs')

a = Analysis(
    [str(ROOT / 'desktop.pyw')],
    pathex=[str(ROOT), str(PYLIBS)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=['tkinter', 'matplotlib', 'numpy', 'scipy', 'pandas', 'IPython', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
              'webview.platforms.qt', 'webview.platforms.gtk', 'webview.platforms.cocoa', 'webview.platforms.android',
              # 程序不用 AVIF、Tk 和 aiohttp；setuptools 是被第三方包顺带拉进来的
              'setuptools', 'pkg_resources', 'aiohttp', 'PIL._avif', 'PIL.AvifImagePlugin', 'PIL._imagingtk', 'PIL.ImageTk'],
    noarchive=False,
)
a.binaries = [item for item in a.binaries if not SKIP_BINARIES.search(item[0].replace('\\', '/'))]
a.datas = [item for item in a.datas if not SKIP_BINARIES.search(item[0].replace('\\', '/'))]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='PandaStudio',   # 1.3.0 起改名（原 WhiteBGStudio）：换文件名才能让 Windows 刷新桌面图标缓存
    icon=str(ROOT / 'packaging' / 'icon' / 'app.ico'),
    version=str(version_file),
    console=False,
    contents_directory='.',
)
coll = COLLECT(exe, a.binaries, a.datas, name='PandaStudio')
