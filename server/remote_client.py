"""本机这一侧的内网渲染机：选渲染位置、自动发现、配对，以及后台把作业转发给渲染机、把结果拉回本机。

做法：选了渲染机以后，工作台写作业时先写一个带 remote 标记的状态文件（本机执行器看到就跳过），
后台线程把模型包和作业发给渲染机，再按渲染机的进度更新本机的状态文件、把出图结果下载回本机任务文件夹。
本机这边的任务、清单、预览、AI 校准、入库流程都不用改：它们看到的还是本机交换目录里的作业和状态。
选回「本机 Max」时，还没发出去的作业交回本机执行器；已经发出去的在渲染机上做完再同步回来。
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import select
import socket
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from .remote_protocol import (DISCOVER_MAGIC, DISCOVERY_PORT, NODE_PORT, SERVICE, ProtocolError, bundle_files,
                              bundle_id, local_path, portable_job, safe_output_rel, write_bundle)

SETTINGS_FILE = 'remote_nodes.json'
TERMINAL = ('done', 'failed')
INFO_TTL = 3


class RemoteError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def parse_address(text):
    """「电脑名」「IP」「电脑名:端口」→ (主机, 端口)。"""
    text = str(text or '').strip()
    if not text or len(text) > 255 or any(c in text for c in '/\\@ '):
        raise RemoteError('请输入渲染机的电脑名或 IP 地址。')
    host, _, port = text.rpartition(':') if text.count(':') == 1 else (text, '', '')
    if not host:
        host, port = text, ''
    try:
        port = int(port) if port else NODE_PORT
    except ValueError:
        raise RemoteError('端口号无效。') from None
    if not 0 < port < 65536:
        raise RemoteError('端口号无效。')
    return host, port


def node_key(host, port):
    return f'{host}:{port}'


class NodeClient:
    def __init__(self, host, port, token=None, timeout=5):
        self.host, self.port, self.token, self.timeout = host, port, token, timeout

    def _connection(self, timeout=None):
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)

    def request(self, method, path, body=None, *, timeout=None, raw=False):
        headers = {'Accept': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        if body is not None and not hasattr(body, 'read'):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        connection = self._connection(timeout)
        try:
            if hasattr(body, 'read'):
                size = os.fstat(body.fileno()).st_size
                headers['Content-Length'] = str(size)
                headers['Content-Type'] = 'application/zip'
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if raw and response.status == 200:
                return response, connection
            content = response.read()
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            raise RemoteError(f'连不上渲染机 {self.host}：{error}') from None
        connection.close()
        data = {}
        if content:
            try:
                data = json.loads(content)
            except ValueError:
                data = {}
        if response.status >= 400:
            raise RemoteError(data.get('error') or f'渲染机返回错误 {response.status}', response.status)
        return data

    def info(self):
        data = self.request('GET', '/node/info', timeout=2)
        if data.get('service') != SERVICE:
            raise RemoteError(f'{self.host} 不是 Panda Studio 渲染机。')
        return data

    def pair(self, code, client_name):
        return self.request('POST', '/node/pair', {'code': code, 'client': client_name})

    def has_bundle(self, bundle):
        try:
            self.request('HEAD', '/node/bundles/' + bundle)
            return True
        except RemoteError as error:
            if error.status == 404:
                return False
            raise

    def put_bundle(self, bundle, path):
        with open(path, 'rb') as stream:
            return self.request('PUT', '/node/bundles/' + bundle, stream, timeout=120)

    def put_job(self, job_id, payload):
        return self.request('PUT', '/node/jobs/' + job_id, payload, timeout=30)

    def job(self, job_id):
        return self.request('GET', '/node/jobs/' + job_id)

    def download(self, job_id, rel, destination):
        response, connection = self.request('GET', f'/node/jobs/{job_id}/file?path=' + quote(rel), timeout=60,
                                            raw=True)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + '.' + uuid.uuid4().hex[:8] + '.part')
        try:
            with temp.open('wb') as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
            os.replace(temp, destination)
        except (OSError, http.client.HTTPException) as error:
            raise RemoteError(f'从渲染机下载结果失败：{error}') from None
        finally:
            temp.unlink(missing_ok=True)
            connection.close()


_FAKE_TUN_NET = ipaddress.ip_network('198.18.0.0/15')


def _is_lan_ipv4(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local and ip not in _FAKE_TUN_NET


def local_lan_ips():
    """收集本机真实局域网 IPv4（排除回环、链路本地及 Clash/代理 TUN 198.18.0.0/15）。"""
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if _is_lan_ipv4(address) and address not in found:
                found.append(address)
    except OSError:
        pass
    for probe in ('10.255.255.255', '172.16.255.255', '192.168.255.255'):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 1))
                address = s.getsockname()[0]
                if _is_lan_ipv4(address) and address not in found:
                    found.append(address)
        except OSError:
            pass
    return found


def broadcast_addresses():
    """本机各局域网网卡所在网段的广播地址（按 /24 估算）和全网广播。"""
    found = ['255.255.255.255']
    for address in local_lan_ips():
        found.append('.'.join(address.split('.')[:3] + ['255']))
    return list(dict.fromkeys(found))


def discover(timeout=1.2, port=DISCOVERY_PORT, targets=None):
    """在内网里喊一声，收集回应的渲染机：{(主机, 端口): 信息}。

    多网卡或开启 Clash/VPN TUN 虚拟网卡时，单独绑定每张真实内网网卡发出广播，
    避免 255.255.255.255 只走虚拟网卡而收不到物理局域网渲染机的回包。
    """
    results = {}
    sockets = []
    send_plan = []

    def add_socket(bind_ip, dests):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((bind_ip, 0))
            sock.setblocking(False)
            sockets.append(sock)
            send_plan.append((sock, list(dict.fromkeys(dests))))
        except OSError:
            sock.close()

    try:
        if targets is not None:
            add_socket('0.0.0.0', list(targets))
        else:
            lan_ips = local_lan_ips()
            for ip in lan_ips:
                subnet_bcast = '.'.join(ip.split('.')[:3] + ['255'])
                add_socket(ip, ['255.255.255.255', subnet_bcast])
            add_socket('0.0.0.0', broadcast_addresses() + ['127.0.0.1'])

        def burst():
            for sock, dests in send_plan:
                for address in dests:
                    try:
                        sock.sendto(DISCOVER_MAGIC, (address, port))
                    except OSError:
                        continue

        burst()
        start = time.monotonic()
        deadline = start + timeout
        second_sent = False
        while sockets:
            now_t = time.monotonic()
            remaining = deadline - now_t
            if remaining <= 0:
                break
            if not second_sent and timeout >= 0.6 and now_t - start >= 0.35:
                burst()
                second_sent = True
            wait = remaining if second_sent or timeout < 0.6 else min(remaining, max(0.05, 0.35 - (now_t - start)))
            try:
                ready, _, _ = select.select(sockets, [], [], wait)
            except (OSError, ValueError):
                break
            for sock in ready:
                try:
                    data, (host, _) = sock.recvfrom(65536)
                except OSError:
                    continue
                try:
                    info = json.loads(data)
                except ValueError:
                    continue
                if isinstance(info, dict) and info.get('service') == SERVICE and isinstance(info.get('port'), int):
                    results[(host, info['port'])] = info
    finally:
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass
    return results


class RemoteSettings:
    """remote_nodes.json：当前渲染位置（None = 本机 Max）和连过的渲染机（含配对令牌）。"""

    def __init__(self, root):
        self.path = Path(root) / SETTINGS_FILE
        self.lock = threading.RLock()

    def read(self):
        from .core import read_json
        try:
            data = read_json(self.path)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        target = data.get('target')
        data['target'] = target if isinstance(target, dict) and target.get('host') else None
        data['nodes'] = data['nodes'] if isinstance(data.get('nodes'), dict) else {}
        return data

    def write(self, data):
        from .core import atomic_json
        atomic_json(self.path, data)

    def node(self, host, port):
        return self.read()['nodes'].get(node_key(host, port))

    def remember(self, host, port, **fields):
        with self.lock:
            data = self.read()
            record = data['nodes'].setdefault(node_key(host, port), {'host': host, 'port': port})
            record.update({k: v for k, v in fields.items() if v is not None})
            self.write(data)
            return record

    def forget(self, host, port):
        with self.lock:
            data = self.read()
            data['nodes'].pop(node_key(host, port), None)
            if data['target'] and node_key(data['target']['host'], data['target']['port']) == node_key(host, port):
                data['target'] = None
            self.write(data)

    def set_target(self, target):
        with self.lock:
            data = self.read()
            data['target'] = target
            self.write(data)


class RemoteBridge:
    def __init__(self, studio):
        self.studio = studio
        self.settings = RemoteSettings(studio.root)
        self.exchange = studio.exchange
        self.jobs_dir = studio.exchange / 'jobs'
        self.cache = studio.exchange / 'remote-cache'
        self.lock = threading.RLock()
        self.active = None              # 还没结束的转发作业 {job_id}
        self._sending = set()           # 正在发送中的作业（防止切换渲染位置时并发删状态导致两边同时跑）
        self.infos = {}                 # {(主机, 端口): (时间, 信息或错误)}
        self.stopping = threading.Event()
        self.thread = None
        self.wake = threading.Event()

    # ── 线程 ──
    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, daemon=True, name='渲染机转发')
            self.thread.start()

    def close(self):
        self.stopping.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=3)

    def _loop(self):
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception as error:  # 后台线程不能停
                import sys
                if sys.stderr is not None:
                    print('渲染机转发出错：' + repr(error), file=sys.stderr, flush=True)
            self.wake.wait(1.5)
            self.wake.clear()

    # ── 渲染位置 ──
    def target(self):
        return self.settings.read()['target']

    def client(self, host, port, timeout=5):
        record = self.settings.node(host, port) or {}
        return NodeClient(host, port, record.get('token'), timeout)

    def node_info(self, host, port, fresh=False):
        """渲染机状态。fresh=False 时只读缓存（界面轮询不等网络），过期由后台线程刷新；还没有缓存时返回「正在连接」。"""
        key = (host, port)
        cached = self.infos.get(key)
        if not fresh:
            if cached is None or time.monotonic() - cached[0] >= INFO_TTL:
                self.wake.set()
            return cached[1] if cached else {'error': '正在连接渲染机…', 'connecting': True}
        try:
            info = self.client(host, port).info()
        except RemoteError as error:
            info = {'error': str(error)}
        self.infos[key] = (time.monotonic(), info)
        return info

    def runner_status(self):
        """渲染位置是渲染机时，给界面的执行器状态（字段和本机执行器一致，另加 target）。"""
        target = self.target()
        info = self.node_info(target['host'], target['port'])
        runner = info.get('runner') or {}
        record = self.settings.node(target['host'], target['port']) or {}
        reachable = 'error' not in info
        online = reachable and info.get('paired') is True and runner.get('online') is True
        return {'online': online, 'age_sec': None, 'busy': online and runner.get('busy') is True, 'job_id': None,
                'max_running': runner.get('max_running') is True, 'max_windows': [], 'max_hidden': 0,
                'can_start': False,
                'target': {'host': target['host'], 'port': target['port'],
                           'name': info.get('name') or record.get('name') or target['host'],
                           'reachable': reachable, 'paired': info.get('paired') is True,
                           'queue': info.get('queue', 0), 'template': info.get('template', True),
                           'error': info.get('error')}}

    def choices(self, scan=True):
        """渲染位置候选：自动发现到的 + 连过的（逐个问一下在不在）。"""
        found = discover() if scan else {}
        known = self.settings.read()['nodes']
        hosts = {(info.get('host') or key.rsplit(':', 1)[0], int(info.get('port') or NODE_PORT))
                 for key, info in known.items()}
        results = {}
        for (host, port), info in found.items():
            results[(host, port)] = info
        threads = []
        for host, port in hosts - set(results):
            def probe(h=host, p=port):
                results[(h, p)] = self.node_info(h, p, fresh=True)
            thread = threading.Thread(target=probe, daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(timeout=3)
        # 同一台渲染机可能既被自动发现（按 IP）、又按电脑名连过：名字相同就算同一台，用连过的那个地址（令牌存在那里）
        by_name = {(record.get('name'), int(record.get('port') or NODE_PORT)): (record.get('host'), int(record.get('port') or NODE_PORT))
                   for record in known.values() if record.get('name') and record.get('token')}
        for (host, port), info in list(results.items()):
            alias = by_name.get((info.get('name'), port))
            if alias and alias != (host, port) and 'error' not in info:
                results.pop((host, port))
                if 'error' in results.get(alias, {'error': True}):
                    results[alias] = info
        target = self.target()
        nodes = []
        for (host, port), info in results.items():
            record = known.get(node_key(host, port)) or {}
            paired = bool(record.get('token'))
            runner = info.get('runner') or {}
            nodes.append({'host': host, 'port': port, 'name': info.get('name') or record.get('name') or host,
                          'reachable': 'error' not in info, 'paired': paired,
                          'max_running': runner.get('max_running') is True, 'online': runner.get('online') is True,
                          'busy': runner.get('busy') is True, 'queue': info.get('queue', 0),
                          'template': info.get('template', True), 'error': info.get('error'),
                          'selected': bool(target and target['host'] == host and target['port'] == port)})
        nodes.sort(key=lambda n: (not n['reachable'], not n['online'], n['name']))
        return {'target': target, 'nodes': nodes}

    def add(self, address):
        host, port = parse_address(address)
        info = self.client(host, port, timeout=3).info()
        self.settings.remember(host, port, name=info.get('name'))
        return {'host': host, 'port': port, 'name': info.get('name') or host}

    def pair(self, host, port, code):
        code = str(code or '').strip()
        if not code.isdigit() or len(code) != 6:
            raise RemoteError('连接码是 6 位数字。')
        result = NodeClient(host, int(port), timeout=5).pair(code, socket.gethostname())
        self.settings.remember(host, int(port), name=result.get('name'), token=result['token'])
        self.node_info(host, int(port), fresh=True)       # 带令牌重新问一次，状态马上显示「已配对」
        return {'host': host, 'port': int(port), 'name': result.get('name') or host}

    def select(self, target):
        """target=None 选回本机 Max；否则 {'host','port'}（必须已配对）。"""
        with self.lock:
            if target is not None:
                host, port = str(target.get('host') or ''), int(target.get('port') or NODE_PORT)
                record = self.settings.node(host, port)
                if not record or not record.get('token'):
                    raise RemoteError('先输入这台渲染机的连接码。', 409)
                target = {'host': host, 'port': port, 'name': record.get('name') or host}
            self.settings.set_target(target)
            self._retarget_pending(target)
        if target is not None:
            self.node_info(target['host'], target['port'], fresh=True)
        self.wake.set()
        return target

    # ── 作业转发 ──
    def _status_path(self, job_id):
        return self.jobs_dir / (job_id + '.status.json')

    def _read(self, path):
        from .core import read_json
        try:
            document = read_json(path)
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def _write_status(self, job_id, status):
        from .core import atomic_json, now
        status['updated_at'] = now()
        atomic_json(self._status_path(job_id), status)

    def _load_active(self):
        if self.active is None:
            active = set()
            for path in self.jobs_dir.glob('*.status.json'):
                status = self._read(path)
                if status and status.get('remote') and status.get('state') not in TERMINAL:
                    active.add(path.name[:-len('.status.json')])
            self.active = active
        return self.active

    def claim(self, job):
        """工作台写作业前调用：渲染位置是渲染机时，先写带 remote 标记的状态文件（本机执行器就不会接这个作业）。"""
        target = self.target()
        if target is None:
            return False
        name = target.get('name') or target['host']
        status = {'job_id': job['job_id'], 'state': 'waiting_lock', 'started_at': None,
                  'items': [{'no': i['no'], 'state': 'pending', 'out_image': i.get('out_image'),
                             'out_mask': i.get('out_mask'), 'message': ''} for i in job.get('items', [])],
                  'message': f'准备发送到渲染机 {name}…', 'log': [],
                  'remote': {'host': target['host'], 'port': target['port'], 'name': name, 'phase': 'pending',
                             'files': {}}}
        with self.lock:
            self._write_status(job['job_id'], status)
            self._load_active().add(job['job_id'])
        self.wake.set()
        return True

    def _retarget_pending(self, target):
        """换了渲染位置：还没发出去的作业跟着换；选回本机就把状态文件删掉，交还本机执行器。正在发送中的跳过。"""
        for job_id in list(self._load_active()):
            if job_id in self._sending:
                continue
            status = self._read(self._status_path(job_id))
            if not status or not status.get('remote') or status['remote'].get('phase') != 'pending':
                continue
            if target is None:
                self._status_path(job_id).unlink(missing_ok=True)
                self.active.discard(job_id)
            else:
                status['remote'].update(host=target['host'], port=target['port'], name=target['name'])
                status['message'] = f'准备发送到渲染机 {target["name"]}…'
                self._write_status(job_id, status)

    def tick(self):
        """后台线程每 1.5 秒一次：刷新当前渲染机状态，把待发送的作业发出去，已发送的同步进度和结果。
        网络操作不拿锁（上传大模型时，界面提交新作业不用等）。"""
        target = self.target()
        if target is not None:
            cached = self.infos.get((target['host'], target['port']))
            if cached is None or time.monotonic() - cached[0] >= INFO_TTL:
                self.node_info(target['host'], target['port'], fresh=True)
        with self.lock:
            job_ids = sorted(self._load_active())
        for job_id in job_ids:
            if self.stopping.is_set():
                return
            action = None
            with self.lock:
                status = self._read(self._status_path(job_id))
                job = self._read(self.jobs_dir / (job_id + '.json'))
                if not status or not status.get('remote') or status.get('state') in TERMINAL or not job:
                    if status is None or not job or status.get('state') in TERMINAL:
                        self.active.discard(job_id)
                    continue
                phase = status['remote'].get('phase')
                if phase in ('pending', 'sending'):
                    if job_id in self._sending:
                        continue
                    status['remote']['phase'] = 'sending'
                    self._write_status(job_id, status)
                    self._sending.add(job_id)
                    action = 'send'
                else:
                    action = 'poll'
            try:
                if action == 'send':
                    self._send(job, status)
                else:
                    self._poll(job, status)
            except RemoteError as error:
                if action == 'send':
                    self._revert_sending(job_id, status)
                self._note_error(job, status, error)
            except ProtocolError as error:
                self._fail(job, status, str(error))
            except Exception:
                if action == 'send':
                    self._revert_sending(job_id, status)
                raise
            finally:
                if action == 'send':
                    with self.lock:
                        self._sending.discard(job_id)

    def _revert_sending(self, job_id, status):
        """发送失败（如渲染机离线）：把 phase 退回 pending，方便重试或切回本机。"""
        with self.lock:
            current = self._read(self._status_path(job_id))
            if current and current.get('remote') and current['remote'].get('phase') == 'sending':
                current['remote']['phase'] = 'pending'
                self._write_status(job_id, current)
                status['remote']['phase'] = 'pending'
            elif status.get('remote') and status['remote'].get('phase') == 'sending':
                status['remote']['phase'] = 'pending'

    def _note_error(self, job, status, error):
        remote = status['remote']
        if error.status == 401:
            message = f'渲染机 {remote["name"]} 需要重新输入连接码（点顶部的渲染位置）。'
        elif error.status in (400, 409, 413) and remote.get('phase') == 'pending':
            return self._fail(job, status, f'渲染机 {remote["name"]} 拒绝了这个作业：{error}')
        elif error.status == 404 and remote.get('phase') == 'sent':
            return self._fail(job, status, f'渲染机 {remote["name"]} 上找不到这个作业（可能已被清理），请重新出图。')
        elif remote.get('phase') == 'pending':
            message = f'渲染机 {remote["name"]} 连不上，恢复后自动发送。'
        else:
            message = f'渲染机 {remote["name"]} 暂时连不上，任务还在那边，连上后继续同步。'
        with self.lock:
            current = self._read(self._status_path(job['job_id']))
            if not current or not current.get('remote') or current.get('state') in TERMINAL:
                return
            if current.get('message') != message:
                current['message'] = message
                self._write_status(job['job_id'], current)

    def _fail(self, job, status, message):
        with self.lock:
            status.update(state='failed', message=message)
            for item in status.get('items', []):
                if item.get('state') not in TERMINAL:
                    item.update(state='failed', message=message)
            self._write_status(job['job_id'], status)
            self.active.discard(job['job_id'])

    def _still_sending_to(self, job_id, host, port):
        current = self._read(self._status_path(job_id))
        remote = current.get('remote') if current else None
        return (bool(current) and bool(remote) and current.get('state') not in TERMINAL
                and remote.get('phase') == 'sending'
                and remote.get('host') == host and remote.get('port') == port)

    def _send(self, job, status):
        remote = status['remote']
        host, port, name = remote['host'], remote['port'], remote['name']
        client = self.client(host, port, timeout=10)
        if not client.token:
            raise RemoteError('需要先配对。', 401)
        task = self.studio.load(job['task_id'])
        base, max_rel, files = bundle_files(job['max_file'], task.get('product_dir'))
        bundle = bundle_id(max_rel, files)
        if not client.has_bundle(bundle):
            size = sum(f[2] for f in files)
            with self.lock:
                if not self._still_sending_to(job['job_id'], host, port):
                    return
                status['message'] = f'正在把模型传到渲染机 {name}（{size / 1024 / 1024:.0f} MB）…'
                self._write_status(job['job_id'], status)
            archive = write_bundle(files, self.cache / 'outgoing' / (bundle + '.zip'))
            try:
                client.put_bundle(bundle, archive)
            finally:
                archive.unlink(missing_ok=True)
        with self.lock:
            if not self._still_sending_to(job['job_id'], host, port):
                return
        payload = {'job': portable_job(job, self.exchange, base, max_rel), 'bundle': bundle}
        client.put_job(job['job_id'], payload)
        with self.lock:
            current = self._read(self._status_path(job['job_id']))
            cur_remote = current.get('remote') if current else None
            if (not current or not cur_remote or current.get('state') in TERMINAL
                    or cur_remote.get('phase') != 'sending'
                    or cur_remote.get('host') != host or cur_remote.get('port') != port):
                return
            cur_remote.update(phase='sent', bundle=bundle)
            current['message'] = f'已发送到渲染机 {name}，排队中。'
            self._write_status(job['job_id'], current)

    def _poll(self, job, status):
        remote = status['remote']
        client = self.client(remote['host'], remote['port'], timeout=10)
        data = client.job(job['job_id'])
        task_id = job['task_id']
        files = remote.setdefault('files', {})
        # 先把结果文件下载回来，再更新状态：本机看到「完成」时文件一定已经在了
        for output in data.get('outputs') or []:
            rel = safe_output_rel(output.get('path', ''))
            stamp = [output.get('size'), output.get('mtime_ns')]
            if files.get(rel) == stamp:
                continue
            destination = local_path(f'@ex:tasks/{task_id}/' + rel, self.exchange, task_id)
            client.download(job['job_id'], rel, destination)
            files[rel] = stamp
        node_status = data.get('status')
        name = remote['name']
        if node_status is None:
            position = data.get('queue_position')
            runner = data.get('runner') or {}
            ahead = f'，前面还有 {position - 1} 个' if position and position > 1 else ''
            if not runner.get('online'):
                message = f'已送到渲染机 {name}{ahead}。那边的 Max 没打开，打开后自动开始。'
            elif ahead:
                message = f'渲染机 {name} 排队中{ahead}。'
            else:
                message = f'渲染机 {name} 马上开始。'
            updated = {**status, 'state': 'waiting_lock', 'message': message}
        else:
            items = []
            for item in node_status.get('items') or []:
                items.append({**item, 'out_image': local_path(item.get('out_image'), self.exchange, task_id),
                              'out_mask': local_path(item.get('out_mask'), self.exchange, task_id)})
            updated = {**node_status, 'job_id': job['job_id'], 'items': items, 'remote': remote}
            if updated.get('state') == 'waiting_lock' and not updated.get('message'):
                updated['message'] = f'渲染机 {name} 上的 Max 正被占用，稍后开始。'
            if updated.get('state') in TERMINAL:
                updated['message'] = (updated.get('message') or '') + f'（在渲染机 {name} 上完成）'
        comparable = lambda s: {k: v for k, v in s.items() if k != 'updated_at'}
        if comparable(updated) != comparable(status):
            self._write_status(job['job_id'], updated)
        if updated.get('state') in TERMINAL:
            with self.lock:
                self.active.discard(job['job_id'])
