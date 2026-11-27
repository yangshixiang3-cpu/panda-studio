"""内网渲染机服务：让公司内网里别的电脑上的 Panda Studio 用这台电脑的 3ds Max 出图。

这台电脑点「设为渲染机」后，服务在后台常驻（开机登录后自动运行，不开窗口）：
- TCP 8775：接收模型包和作业、报告进度、回传结果（只接受内网地址，配对后才能提交）；
- UDP 8776：回应内网里的「自动发现」广播，告诉对方这台电脑的名字、Max 开没开、忙不忙。
收到的作业写进这台电脑自己的交换目录，由这台电脑 Max 里的白底执行器照常处理；模板、渲染预设用这台电脑自己的。
每个来访电脑的东西放在各自的命名空间（remote-<来访编号>-…）里，14 天没用自动清理。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .remote_protocol import (BUNDLE_ID_RE, BUNDLE_MAX_BYTES, DISCOVER_MAGIC, DISCOVERY_PORT, JOB_ID_RE, NODE_PORT,
                              PROTOCOL, SERVICE, ProtocolError, extract_bundle, inside, node_job, portable_status,
                              safe_rel)

STATE_FILE = 'render_node.json'
KEEP_DAYS = 14
PAIR_FAILURE_LIMIT = 5
PAIR_GLOBAL_FAILURE_LIMIT = 20
PAIR_WINDOW_SEC = 600
PAIR_LOCK_SEC = 600
PAIR_CODE_TTL_SEC = 600
HANDLER_TIMEOUT_SEC = 15
MAX_CONCURRENT_REQUESTS = 16
TERMINAL = ('done', 'failed')
LOG_BYTES = 1024 * 1024
NODE_JOB_RE = re.compile(r'^(\d{8}-\d{6}-[0-9a-f]{4})-([0-9a-f]{8})$')
SKIP_OUTPUT_SUFFIXES = ('.tmp', '.prev', '.part')


class NodeError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _read_json(path):
    from .core import read_json
    return read_json(path)


def _atomic_json(path, data, immutable=False):
    from .core import atomic_json
    atomic_json(path, data, immutable=immutable)


def _now():
    from .core import now
    return now()


def epoch(value):
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def allowed_address(address):
    """只接受公司内网（私有地址、本机、链路本地）。"""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


_FAKE_TUN_NET = ipaddress.ip_network('198.18.0.0/15')
_RFC1918_NETS = (
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
)


def is_lan_ipv4(address):
    """判断是否适合作为内网直连 IPv4（排除回环、链路本地 APIPA 及 Clash/代理 TUN 198.18.0.0/15）。"""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version != 4 or ip.is_loopback or ip.is_link_local or ip in _FAKE_TUN_NET:
        return False
    return ip.is_private


def local_addresses():
    """这台电脑的内网 IPv4 地址（给设置页显示，方便手动连接；RFC1918 局域网地址排前面，过滤代理 TUN 假地址）。"""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if is_lan_ipv4(address):
                found.add(address)
    except OSError:
        pass
    for probe in ('10.255.255.255', '172.16.255.255', '192.168.255.255'):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 1))
                address = s.getsockname()[0]
                if is_lan_ipv4(address):
                    found.add(address)
        except OSError:
            pass
    return sorted(found, key=lambda a: (0 if any(ipaddress.ip_address(a) in n for n in _RFC1918_NETS) else 1, a))


def token_key(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _future(seconds):
    return (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat(timespec='seconds')


class NodeState:
    """render_node.json：是否开启、连接码、已配对的电脑（只存令牌的哈希）。工作台和后台服务两个进程都会读写。"""

    def __init__(self, root):
        self.path = Path(root) / STATE_FILE
        self.lock = threading.RLock()
        self._cache = (None, None)

    def read(self):
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            stamp = None
        cached_stamp, cached = self._cache
        if stamp is not None and stamp == cached_stamp:
            return json.loads(json.dumps(cached))
        try:
            data = _read_json(self.path)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data['enabled'] = data.get('enabled') is True
        data['clients'] = data['clients'] if isinstance(data.get('clients'), dict) else {}
        self._cache = (stamp, data)
        return json.loads(json.dumps(data))

    def write(self, data):
        _atomic_json(self.path, data)
        self._cache = (None, None)

    @staticmethod
    def code_valid(data, now_ts=None):
        if not re.fullmatch(r'\d{6}', str(data.get('code', ''))):
            return False
        expires = epoch(data.get('code_expires_at'))
        return expires is not None and expires > (time.time() if now_ts is None else now_ts)

    @staticmethod
    def _set_new_code(data):
        old = data.get('code')
        code = f'{secrets.randbelow(10 ** 6):06d}'
        while code == old:
            code = f'{secrets.randbelow(10 ** 6):06d}'
        data['code'] = code
        data['code_expires_at'] = _future(PAIR_CODE_TTL_SEC)
        return data

    def ensure_code(self):
        with self.lock:
            data = self.read()
            if not self.code_valid(data):
                self._set_new_code(data)
                self.write(data)
            return data

    def regenerate_code(self):
        with self.lock:
            data = self.read()
            self._set_new_code(data)
            self.write(data)
            return data

    def set_enabled(self, value):
        with self.lock:
            data = self.ensure_code()
            data['enabled'] = bool(value)
            self.write(data)
            return data

    def forget_clients(self):
        with self.lock:
            data = self.read()
            data['clients'] = {}
            self.write(data)
            return data

    def pair(self, code, client_name):
        with self.lock:
            data = self.read()
            if not self.code_valid(data):
                # 连接码已过期：生成新码并拒绝旧码
                self._set_new_code(data)
                self.write(data)
                return None
            if not secrets.compare_digest(str(code).strip(), data['code']):
                return None
            token = secrets.token_urlsafe(24)
            data['clients'][token_key(token)] = {'name': str(client_name or '未命名电脑')[:60], 'paired_at': _now()}
            # 配对成功后自动轮换连接码，不影响已配对令牌
            self._set_new_code(data)
            self.write(data)
            return token

    def client(self, token):
        """令牌有效时返回（来访编号 8 位, 记录）。"""
        if not token or len(token) > 200:
            return None
        key = token_key(token)
        record = self.read()['clients'].get(key)
        return (key[:8], record) if isinstance(record, dict) else None


class RenderNode:
    def __init__(self, root, config=None):
        from .core import TEMPLATE_FILE
        from .runner import RunnerStatus

        self.root = Path(root).resolve()
        self.config = config or _read_json(self.root / 'config.json')
        self.exchange = (self.root / self.config['exchange_dir']).resolve()
        (self.exchange / 'jobs').mkdir(parents=True, exist_ok=True)
        (self.exchange / 'tasks').mkdir(parents=True, exist_ok=True)
        self.cache = self.exchange / 'remote-cache'
        self.template_file = (self.root / TEMPLATE_FILE).resolve()
        self.state = NodeState(self.root)
        self.runner = RunnerStatus(self.root, self.exchange, self.config)
        self.lock = threading.RLock()
        self.failures = {}
        self.global_failures = []
        self.pair_locked_until = 0.0
        self.name = socket.gethostname()
        self.log_path = self.exchange / 'render_node.log'

    # ── 日志 ──
    def log(self, message):
        try:
            if self.log_path.is_file() and self.log_path.stat().st_size > LOG_BYTES:
                os.replace(self.log_path, self.log_path.with_name('render_node.log.1'))
            with self.log_path.open('a', encoding='utf-8') as stream:
                stream.write(f'{_now()} {message}\n')
        except OSError:
            pass

    # ── 状态 ──
    def preset(self):
        value = self.config.get('render_preset_rps')
        return (self.root / value).resolve() if value else None

    def _jobs(self):
        """这台电脑交换目录里没结束的作业：[(排序键, 作业号)]，顺序和执行器处理顺序一致。"""
        entries = []
        for path in (self.exchange / 'jobs').glob('*.json'):
            if path.name.endswith('.status.json'):
                continue
            status_path = path.with_name(path.stem + '.status.json')
            try:
                status = _read_json(status_path) if status_path.is_file() else None
                if status and (status.get('state') in TERMINAL or status.get('remote')):
                    continue
                created = epoch(_read_json(path).get('created_at'))
            except (OSError, ValueError, AttributeError):
                continue
            entries.append(((created if created is not None else 0, path.name), path.stem))
        return [name for _, name in sorted(entries)]

    def info(self, client=None):
        status = self.runner.status()
        return {'service': SERVICE, 'protocol': PROTOCOL, 'name': self.name,
                'runner': {'online': status['online'], 'busy': status['busy'], 'max_running': status['max_running']},
                'queue': len(self._jobs()), 'template': self.template_file.is_file(), 'paired': client is not None}

    # ── 配对 ──
    def pair(self, address, code, client_name):
        now = time.time()
        with self.lock:
            for ip in list(self.failures):
                kept = [t for t in self.failures[ip] if now - t < PAIR_WINDOW_SEC]
                if kept:
                    self.failures[ip] = kept
                else:
                    del self.failures[ip]
            self.global_failures = [t for t in self.global_failures if now - t < PAIR_WINDOW_SEC]
            if now < self.pair_locked_until:
                raise NodeError('连接码输错次数太多，配对已暂停，请 10 分钟后再试。', 429)
            attempts = self.failures.get(address, [])
            if len(attempts) >= PAIR_FAILURE_LIMIT:
                raise NodeError('连接码输错次数太多，请 10 分钟后再试。', 429)
            token = self.state.pair(code, client_name)
            if token is None:
                attempts.append(now)
                self.failures[address] = attempts
                self.global_failures.append(now)
                if len(self.global_failures) >= PAIR_GLOBAL_FAILURE_LIMIT:
                    self.pair_locked_until = now + PAIR_LOCK_SEC
                raise NodeError('连接码不对或已过期。请看渲染机「本机设置」里显示的 6 位连接码。', 403)
            self.failures.pop(address, None)
        self.log(f'配对：{client_name}（{address}）')
        return {'token': token, 'name': self.name}

    # ── 模型包 ──
    def bundle_dir(self, client_key, bundle):
        if not BUNDLE_ID_RE.fullmatch(bundle or ''):
            raise NodeError('模型包编号无效。')
        return self.cache / 'bundles' / client_key / bundle

    def has_bundle(self, client_key, bundle):
        return self.bundle_dir(client_key, bundle).is_dir()

    def put_bundle(self, client_key, bundle, stream, length):
        target = self.bundle_dir(client_key, bundle)
        if not 0 < length <= BUNDLE_MAX_BYTES * 1.2:
            raise NodeError('模型包大小无效。', 413)
        incoming = self.cache / 'incoming'
        incoming.mkdir(parents=True, exist_ok=True)
        temp = incoming / (uuid.uuid4().hex + '.zip')
        try:
            remaining = length
            with temp.open('wb') as output:
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise NodeError('模型包上传不完整。')
                    output.write(chunk)
                    remaining -= len(chunk)
            if target.is_dir():
                return {'bundle': bundle, 'cached': True}
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                extract_bundle(temp, target)
            except ProtocolError as error:
                raise NodeError(str(error)) from None
        finally:
            temp.unlink(missing_ok=True)
        self.log(f'收到模型包 {bundle[:12]}（{length // 1024} KB）')
        return {'bundle': bundle, 'cached': False}

    # ── 作业 ──
    @staticmethod
    def node_job_id(client_key, job_id):
        if not JOB_ID_RE.fullmatch(job_id or ''):
            raise NodeError('作业号无效。', 404)
        return f'{job_id}-{client_key}'

    def submit(self, client_key, client_name, job_id, payload):
        node_job_id = self.node_job_id(client_key, job_id)
        path = self.exchange / 'jobs' / (node_job_id + '.json')
        if path.is_file():                       # 网络断开后重发：同一个作业只收一次
            return {'job_id': job_id, 'accepted': True, 'resent': True}
        portable = payload.get('job') if isinstance(payload, dict) else None
        if not isinstance(portable, dict) or portable.get('job_id') != job_id:
            raise NodeError('作业格式无效。')
        bundle_dir = self.bundle_dir(client_key, str(payload.get('bundle', '')))
        if not bundle_dir.is_dir():
            raise NodeError('渲染机上还没有这个模型包。', 409)
        if not self.template_file.is_file():
            raise NodeError('这台渲染机上没有白底模板（安装目录 template\\白底模板.max），请在渲染机上放好模板。', 409)
        task_id = str(portable.get('task_id', ''))
        if not JOB_ID_RE.fullmatch(task_id):
            raise NodeError('任务号无效。')
        node_task_id = f'remote-{client_key}-{task_id}'
        try:
            job, folders = node_job(portable, node_exchange=self.exchange, node_job_id=node_job_id,
                                    node_task_id=node_task_id, bundle_dir=bundle_dir,
                                    template=self.template_file, preset=self.preset())
        except ProtocolError as error:
            raise NodeError(str(error)) from None
        # 按收到的先后排队（不用发送方电脑的时钟）
        job.update(created_at=_now(), remote={'client': str(client_name or '')[:60], 'client_job_id': job_id})
        for folder in folders:
            folder.mkdir(parents=True, exist_ok=True)
        try:
            os.utime(bundle_dir)
        except OSError:
            pass
        _atomic_json(path, job, immutable=True)
        self.log(f'收到作业 {job_id}（{job["type"]}{" " + job["mode"] if job.get("mode") else ""}）来自 {client_name}')
        return {'job_id': job_id, 'accepted': True, 'resent': False}

    def _job(self, client_key, job_id):
        node_job_id = self.node_job_id(client_key, job_id)
        path = self.exchange / 'jobs' / (node_job_id + '.json')
        if not path.is_file():
            raise NodeError('渲染机上没有这个作业（可能已被清理）。', 404)
        job = _read_json(path)
        status_path = path.with_name(node_job_id + '.status.json')
        status = _read_json(status_path) if status_path.is_file() else None
        return node_job_id, job, status

    def job_status(self, client_key, job_id):
        node_job_id, job, status = self._job(client_key, job_id)
        workspace = self.exchange / 'tasks' / job['task_id']
        client_task = job['task_id'][len(f'remote-{client_key}-'):]
        pending = self._jobs()
        position = pending.index(node_job_id) + 1 if node_job_id in pending else None
        runner = self.runner.status()
        return {'state': status['state'] if status else 'queued',
                'status': portable_status(status, workspace, client_task) if status else None,
                'outputs': self._outputs(workspace, status),
                'queue_position': position,
                'runner': {'online': runner['online'], 'busy': runner['busy'], 'max_running': runner['max_running']}}

    @staticmethod
    def _outputs(workspace, status):
        """本作业写出的文件：工作区里在作业开始之后改过的文件（执行器一次只做一个作业）。"""
        started = epoch((status or {}).get('started_at'))
        if started is None or not workspace.is_dir():
            return []
        found = []
        for directory, _, names in os.walk(workspace):
            for name in names:
                if name.startswith('.') or name.endswith(SKIP_OUTPUT_SUFFIXES):
                    continue
                path = Path(directory) / name
                try:
                    info = path.stat()
                except OSError:
                    continue
                if info.st_mtime >= started - 2:
                    found.append({'path': path.relative_to(workspace).as_posix(), 'size': info.st_size,
                                  'mtime_ns': info.st_mtime_ns})
        return sorted(found, key=lambda f: f['path'])

    def output_file(self, client_key, job_id, rel):
        _, job, _ = self._job(client_key, job_id)
        workspace = self.exchange / 'tasks' / job['task_id']
        try:
            path = workspace / safe_rel(rel)
        except ProtocolError as error:
            raise NodeError(str(error), 403) from None
        if not inside(path, workspace) or not path.is_file():
            raise NodeError('没有这个文件。', 404)
        return path

    def cancel(self, client_key, job_id):
        node_job_id, _, status = self._job(client_key, job_id)
        if status and (status.get('state') != 'waiting_lock' or status.get('started_at')):
            raise NodeError('已经开始渲染，不能取消。', 409)
        for suffix in ('.json', '.status.json'):
            (self.exchange / 'jobs' / (node_job_id + suffix)).unlink(missing_ok=True)
        return {'cancelled': True}

    # ── 清理 ──
    def cleanup(self, now_ts=None):
        """14 天没动过的来访作业、工作区、模型包删掉（只动 remote-* 命名空间）。"""
        limit = (now_ts or time.time()) - KEEP_DAYS * 86400
        removed = 0
        for path in list((self.exchange / 'jobs').glob('*.json')):
            stem = path.name[:-len('.status.json')] if path.name.endswith('.status.json') else path.stem
            if not NODE_JOB_RE.fullmatch(stem):
                continue
            try:
                if path.stat().st_mtime < limit:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        for folder in list((self.exchange / 'tasks').glob('remote-*')):
            removed += self._remove_if_stale(folder, limit)
        bundles = self.cache / 'bundles'
        if bundles.is_dir():
            for client in bundles.iterdir():
                for folder in list(client.iterdir()) if client.is_dir() else []:
                    removed += self._remove_if_stale(folder, limit, newest=False)
        return removed

    @staticmethod
    def _remove_if_stale(folder, limit, newest=True):
        try:
            if not folder.is_dir():
                return 0
            stamp = folder.stat().st_mtime
            if newest:
                for directory, _, names in os.walk(folder):
                    for name in names:
                        stamp = max(stamp, (Path(directory) / name).stat().st_mtime)
            if stamp < limit:
                shutil.rmtree(folder)
                return 1
        except OSError:
            pass
        return 0


# ── HTTP ────────────────────────────────────────────────

class NodeServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, max_concurrent=MAX_CONCURRENT_REQUESTS):
        self.concurrency = threading.Semaphore(max_concurrent)
        super().__init__(server_address, RequestHandlerClass)

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request(self, request, client_address):
        if not self.concurrency.acquire(timeout=HANDLER_TIMEOUT_SEC):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.concurrency.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.concurrency.release()


def node_handler(node):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'PandaStudioNode/1'
        timeout = HANDLER_TIMEOUT_SEC

        def log_message(self, fmt, *args):
            pass

        def reply(self, data, status=200):
            content = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(content)

        def client(self, required=True):
            auth = self.headers.get('Authorization', '')
            found = node.state.client(auth[7:].strip() if auth.startswith('Bearer ') else '')
            if found is None and required:
                raise NodeError('需要先输入连接码配对。', 401)
            return found

        def json_body(self, limit=5_000_000):
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= limit:
                raise NodeError('请求内容大小无效。', 413)
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise NodeError('请求内容必须是 JSON 对象。')
            return value

        def send_path(self, path):
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(path.stat().st_size))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            with path.open('rb') as stream:
                shutil.copyfileobj(stream, self.wfile, 1024 * 1024)

        def dispatch(self):
            if not allowed_address(self.client_address[0]):
                raise NodeError('只接受公司内网访问。', 403)
            url = urlsplit(self.path)
            parts = unquote(url.path).strip('/').split('/')
            query = parse_qs(url.query)
            method = self.command
            if parts[0] != 'node':
                raise NodeError('接口不存在。', 404)
            if parts == ['node', 'info'] and method in ('GET', 'HEAD'):
                return self.reply(node.info(self.client(required=False)))
            if parts == ['node', 'pair'] and method == 'POST':
                data = self.json_body(10_000)
                return self.reply(node.pair(self.client_address[0], data.get('code', ''), data.get('client', '')))
            key, record = self.client()
            if len(parts) == 3 and parts[1] == 'bundles':
                if method == 'HEAD':
                    if node.has_bundle(key, parts[2]):
                        return self.reply({'bundle': parts[2]})
                    raise NodeError('没有这个模型包。', 404)
                if method == 'PUT':
                    return self.reply(node.put_bundle(key, parts[2], self.rfile,
                                                      int(self.headers.get('Content-Length', '0'))), 201)
            if len(parts) >= 3 and parts[1] == 'jobs':
                job_id = parts[2]
                if len(parts) == 3:
                    if method == 'PUT':
                        return self.reply(node.submit(key, record.get('name'), job_id, self.json_body()), 201)
                    if method == 'GET':
                        return self.reply(node.job_status(key, job_id))
                    if method == 'DELETE':
                        return self.reply(node.cancel(key, job_id))
                if len(parts) == 4 and parts[3] == 'file' and method == 'GET':
                    return self.send_path(node.output_file(key, job_id, query.get('path', [''])[0]))
            raise NodeError('接口不存在。', 404)

        def safe_dispatch(self):
            try:
                self.dispatch()
            except NodeError as error:
                self.reply({'error': str(error)}, error.status)
            except (json.JSONDecodeError, ValueError, KeyError, TypeError) as error:
                self.reply({'error': '请求格式无效：' + str(error)}, 400)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                pass
            except Exception as error:
                node.log('服务错误：' + repr(error))
                try:
                    self.reply({'error': '渲染机内部出错，请查看渲染机日志。'}, 500)
                except OSError:
                    pass

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = safe_dispatch
    return Handler


class Discovery(threading.Thread):
    """回应自动发现广播：收到 DISCOVER_MAGIC 就把本机信息发回去。"""

    def __init__(self, node, port, http_port, host='0.0.0.0'):
        super().__init__(daemon=True, name='渲染机发现')
        self.node, self.http_port = node, http_port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.socket.bind((host, port))
        self.socket.settimeout(0.5)
        self.port = self.socket.getsockname()[1]
        self.stopping = threading.Event()

    def run(self):
        while not self.stopping.is_set():
            try:
                data, address = self.socket.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                if self.stopping.is_set():
                    break
                continue
            if data.strip() != DISCOVER_MAGIC or not allowed_address(address[0]):
                continue
            try:
                info = {**self.node.info(), 'port': self.http_port}
                self.socket.sendto(json.dumps(info, ensure_ascii=False).encode('utf-8'), address)
            except OSError:
                continue

    def close(self):
        self.stopping.set()
        try:
            self.socket.close()
        except OSError:
            pass


class NodeService:
    def __init__(self, node, server, discovery):
        self.node, self.server, self.discovery = node, server, discovery
        self.stopping = threading.Event()
        self.threads = [threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.2}, daemon=True,
                                         name='渲染机服务'),
                        threading.Thread(target=self._cleanup_loop, daemon=True, name='渲染机清理')]
        if discovery is not None:
            self.threads.append(discovery)
        for thread in self.threads:
            thread.start()

    @property
    def port(self):
        return self.server.server_port

    def _cleanup_loop(self):
        while not self.stopping.wait(60):
            try:
                self.node.cleanup()
            except Exception as error:
                self.node.log('清理失败：' + repr(error))
            self.stopping.wait(6 * 3600)

    def close(self):
        self.stopping.set()
        if self.discovery is not None:
            self.discovery.close()
        self.server.shutdown()
        self.server.server_close()


def serve(root, host='0.0.0.0', port=NODE_PORT, discovery_port=DISCOVERY_PORT, config=None):
    """启动渲染机服务（端口被占用会抛 OSError）。discovery_port=None 时不开自动发现。"""
    node = RenderNode(root, config)
    node.state.ensure_code()
    server = NodeServer((host, port), node_handler(node))
    try:
        discovery = Discovery(node, discovery_port, server.server_port, host) if discovery_port is not None else None
    except OSError:
        server.server_close()
        raise
    node.log(f'渲染机服务启动：{node.name} 端口 {server.server_port}')
    return NodeService(node, server, discovery)


def run_forever(root):
    """后台常驻入口（PandaStudio.exe --render-node）：没开启就直接退出；已经在运行（端口占用）也退出。
    「停止共享」把 enabled 改成 false 后，几秒内自己停下。"""
    state = NodeState(root)
    if not state.read().get('enabled'):
        return 0
    try:
        service = serve(root)
    except OSError:
        return 0
    try:
        while state.read().get('enabled'):
            time.sleep(3)
    finally:
        service.node.log('渲染机服务停止')
        service.close()
    return 0


if __name__ == '__main__':
    from .core import ROOT
    sys.exit(run_forever(ROOT))
