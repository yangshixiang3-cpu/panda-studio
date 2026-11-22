"""一键把这台电脑设为内网渲染机，以及停止共享。

「设为渲染机」依次做：
1. 找到这台电脑的 3ds Max 并装好连接插件（Max 一打开白底执行器就自动启动；找不到 Max 就停下，什么都不改）；
2. 开启渲染机服务，生成 6 位连接码；
3. 登记开机自启（当前用户登录 Windows 后在后台运行，不开窗口，不需要管理员）；
4. 防火墙放行 TCP 8775（服务）和 UDP 8776（自动发现），只放行内网地址。这一步需要管理员，Windows 会弹一次确认；
5. 立即在后台启动服务。
「停止共享」：关掉开关（服务几秒内自己停下）、取消开机自启。防火墙规则保留（删除也要管理员；服务停了端口就没人监听）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from .remote_protocol import DISCOVERY_PORT, NODE_PORT

RULE_NAME = 'Panda Studio Render Node'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_VALUE = 'PandaStudioRenderNode'
PRIVATE_RANGES = 'LocalSubnet,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16'


def service_command(root):
    """后台服务的启动命令：安装版用 PandaStudio.exe --render-node；源码运行用 pythonw desktop.pyw --render-node。"""
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--render-node']
    exe = Path(sys.executable)
    pythonw = exe.with_name('pythonw.exe')
    return [str(pythonw if pythonw.is_file() else exe), str(Path(root) / 'desktop.pyw'), '--render-node']


def service_running(port=NODE_PORT):
    from .remote_client import NodeClient, RemoteError
    try:
        NodeClient('127.0.0.1', port, timeout=1).info()
        return True
    except RemoteError:
        return False


def start_service(root, wait=8):
    if service_running():
        return True
    options = {}
    if os.name == 'nt':
        options['creationflags'] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                    | subprocess.CREATE_NO_WINDOW)
    subprocess.Popen(service_command(root), cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, **options)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if service_running():
            return True
        time.sleep(0.3)
    return False


def ensure_service(root):
    """工作台启动时：这台电脑设为渲染机了但服务没在跑（比如刚升级），顺手拉起来。"""
    from .render_node import NodeState
    try:
        if NodeState(root).read().get('enabled') and not service_running():
            start_service(root, wait=0)
    except OSError:
        pass


# ── 开机自启（当前用户的 Run 键，不需要管理员） ──

def autostart_enabled(root):
    if os.name != 'nt':
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
        return value == subprocess.list2cmdline(service_command(root))
    except OSError:
        return False


def set_autostart(root, enabled):
    if os.name != 'nt':
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(service_command(root)))
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


# ── 防火墙 ──

def firewall_rule_exists():
    if os.name != 'nt':
        return True
    try:
        result = subprocess.run(['netsh', 'advfirewall', 'firewall', 'show', 'rule', f'name={RULE_NAME}'],
                                capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def firewall_commands():
    base = (f'netsh advfirewall firewall add rule name="{RULE_NAME}" dir=in action=allow profile=any '
            f'remoteip={PRIVATE_RANGES}')
    return f'{base} protocol=TCP localport={NODE_PORT} & {base} protocol=UDP localport={DISCOVERY_PORT}'


def add_firewall_rule():
    """以管理员身份运行 netsh（Windows 弹一次确认）。返回 (成功, 说明)。"""
    if os.name != 'nt':
        return True, ''
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('fMask', ctypes.c_ulong), ('hwnd', wintypes.HWND),
                    ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
                    ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
                    ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
                    ('dwHotKey', wintypes.DWORD), ('hIconOrMonitor', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40                     # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = 'runas'
    info.lpFile = 'cmd.exe'
    info.lpParameters = '/c ' + firewall_commands()
    info.nShow = 0
    shell32, kernel32 = ctypes.windll.shell32, ctypes.windll.kernel32
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        if ctypes.GetLastError() == 1223:
            return False, '没有同意管理员确认，防火墙没放行：别的电脑可能连不上。可以再点一次「重新放行防火墙」。'
        return False, '防火墙设置没能运行。'
    try:
        kernel32.WaitForSingleObject(info.hProcess, 60000)
    finally:
        kernel32.CloseHandle(info.hProcess)
    if firewall_rule_exists():
        return True, ''
    return False, '防火墙规则没加上（可能被公司策略禁止），请找 IT 放行 TCP 8775 和 UDP 8776。'


# ── 设置页用的整体状态 / 开关 ──

def status(studio):
    from .render_node import NodeState, local_addresses
    from .settings import MachineSettings

    state = NodeState(studio.root).read()
    machine = MachineSettings(studio).read()
    running = service_running()
    runner = studio.runner.status()
    return {'enabled': state['enabled'], 'running': running, 'code': state.get('code') if state['enabled'] else None,
            'name': socket.gethostname(), 'addresses': local_addresses(), 'port': NODE_PORT,
            'clients': sorted(({'name': c.get('name', ''), 'paired_at': c.get('paired_at', '')}
                               for c in state['clients'].values()), key=lambda c: c['paired_at']),
            'checks': {'max_exe': machine['checks']['max_exe'], 'plugin': bool((machine.get('plugin') or {}).get('installed')),
                       'template': machine['checks']['template'], 'runner_online': runner['online'],
                       'firewall': firewall_rule_exists() if state['enabled'] else None,
                       'autostart': autostart_enabled(studio.root) if state['enabled'] else None}}


def enable(studio):
    from .core import StudioError
    from .render_node import NodeState
    from .settings import MachineSettings

    machine = MachineSettings(studio)
    machine.connect_max()                    # 找不到 Max 会抛 StudioError，后面的都不做
    NodeState(studio.root).set_enabled(True)
    notes = []
    try:
        set_autostart(studio.root, True)
    except OSError as error:
        notes.append('开机自启没登记上：' + str(error))
    if not firewall_rule_exists():
        ok, message = add_firewall_rule()
        if not ok:
            notes.append(message)
    if not start_service(studio.root):
        notes.append('渲染机服务没启动起来：端口 8775 可能被别的程序占用。')
    result = status(studio)
    if not result['checks']['template']:
        notes.append('这台电脑缺白底模板（安装目录 template\\白底模板.max），放好之前别的电脑提交的任务会被拒绝。')
    result['message'] = '；'.join(notes) if notes else '已设为渲染机。Max 打开时，内网里别的电脑就能选这台出图。'
    if not result['running']:
        raise StudioError(result['message'], 500)
    return result


def disable(studio):
    from .render_node import NodeState
    NodeState(studio.root).set_enabled(False)
    try:
        set_autostart(studio.root, False)
    except OSError:
        pass
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and service_running():
        time.sleep(0.3)
    result = status(studio)
    result['message'] = '已停止共享，别的电脑不能再用这台出图。'
    return result


def regenerate_code(studio):
    from .render_node import NodeState
    NodeState(studio.root).regenerate_code()
    return status(studio)


def forget_clients(studio):
    from .render_node import NodeState
    NodeState(studio.root).forget_clients()
    return status(studio)


def retry_firewall(studio):
    ok, message = add_firewall_rule()
    result = status(studio)
    result['message'] = '防火墙已放行。' if ok else message
    return result
