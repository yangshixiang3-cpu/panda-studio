# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 配置：把白底工作台（桌面窗口 + 本地服务 + 中转站 SDK）打成一个文件夹。
# 构建：在 whitebg-studio 目录运行 packaging\build.ps1
# 程序和数据都放在安装目录（contents_directory='.'），所以 server.core.ROOT 就是 exe 所在目录。
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
PYLIBS = ROOT / 'pylibs'
# 中转站 SDK（openai 及依赖）装在 pylibs；先加进搜索路径，收集子模块时才找得到
sys.path.insert(0, str(PYLIBS))
sys.path.insert(0, str(ROOT))
SKIP_DIRS = {'__pycache__', 'tests', 'tmp'}
SKIP_SUFFIX = {'.bak', '.prev', '.pyc', '.tmp'}


def tree(folder):
    """把 ROOT/folder 下的文件按原相对路径放到程序目录旁边。"""
    items = []
    for path in sorted((ROOT / folder).rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if SKIP_DIRS & set(rel.parts) or path.suffix.lower() in SKIP_SUFFIX:
            continue
        items.append((str(path), str(rel.parent)))
    return items


datas = tree('web') + tree('max') + tree('refs') + tree('template') + [(str(ROOT / 'config.json'), '.')]
# 服务端按 ROOT/server/ 读取的数据文件（如 AI 校准的输出结构 calibration_schema.json）。
# 代码本身打进了 PYZ，这些 .json 不会自动带上；1.1.0 漏了它，AI 校准在安装版里报「找不到 calibration_schema.json」。
datas += [(str(path), 'server') for path in sorted((ROOT / 'server').glob('*.json'))]
if not any(Path(source).name == 'calibration_schema.json' for source, _ in datas):
    raise SystemExit('没有收集到 server/calibration_schema.json')

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
              'webview.platforms.qt', 'webview.platforms.gtk', 'webview.platforms.cocoa', 'webview.platforms.android'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='PandaStudio',   # 1.3.0 起改名（原 WhiteBGStudio）：换文件名才能让 Windows 刷新桌面图标缓存
    icon=str(ROOT / 'packaging' / 'icon' / 'app.ico'),
    console=False,
    contents_directory='.',
)
coll = COLLECT(exe, a.binaries, a.datas, name='PandaStudio')
