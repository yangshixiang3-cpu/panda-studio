"""校准引擎设置与当前 Windows 用户的 DPAPI 密钥。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import ipaddress
import os
from pathlib import Path
import threading
import uuid
from urllib.parse import urlsplit


class AIError(ValueError):
    pass


class DataBlob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data, *, decrypt=False):
    if os.name != 'nt':
        raise AIError('仅支持 Windows 存储密钥')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = DataBlob(len(buffer), buffer), DataBlob()
    # CRYPTPROTECT_UI_FORBIDDEN，默认使用当前用户范围。
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise AIError('无法读取已保存的 API Key' if decrypt else '无法加密 API Key')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(target.pbData)


def _is_loopback_host(host):
    if not host:
        return False
    if host.lower() == 'localhost':
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def url_origin(value):
    parsed = urlsplit(str(value or '').strip())
    if not parsed.scheme or not parsed.hostname:
        return ''
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    port = parsed.port or (443 if scheme == 'https' else 80 if scheme == 'http' else None)
    host_part = f'[{host}]' if ':' in host else host
    return f'{scheme}://{host_part}:{port}' if port is not None else f'{scheme}://{host_part}'


def normalize_url(value):
    if not isinstance(value, str):
        raise AIError('请输入 https:// 接口地址')
    value = value.strip().rstrip('/')
    if not value:
        return ''
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or '?' in value or '#' in value or any(c.isspace() or ord(c) < 32 for c in value)
                or parsed.port == 0):
            raise ValueError
    except ValueError:
        raise AIError('请输入有效的 https:// 接口地址') from None
    if parsed.scheme == 'http' and not _is_loopback_host(parsed.hostname):
        raise AIError('公网接口地址必须使用 https://（仅本机地址允许 http://）')
    return value


def _is_app_root(root):
    from .core import ROOT
    try:
        return Path(root).resolve() == ROOT.resolve()
    except OSError:
        return False


def _migrate_file(source, target):
    """复制密钥文件 → 校验字节完全一致 → 仅校验通过后删除旧文件；目标已存在时跳过。"""
    if not source.is_file() or target.exists():
        return False
    data = source.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f'{uuid.uuid4().hex}.tmp')
    try:
        temp.write_bytes(data)
        if temp.read_bytes() != data:
            raise OSError('密钥迁移校验不一致')
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    if target.read_bytes() != data:
        raise OSError('密钥迁移校验不一致')
    source.unlink()
    return True


def migrate_legacy_secrets(secrets_dir, legacy_dir=None):
    """将旧版 %APPDATA%\\whitebg-studio\\ 里的 DPAPI 密钥安全迁移到 <root>\\secrets\\。"""
    if legacy_dir is None:
        appdata = os.environ.get('APPDATA')
        if os.name != 'nt' or not appdata:
            return 0
        legacy_dir = Path(appdata) / 'whitebg-studio'
    else:
        legacy_dir = Path(legacy_dir)
    secrets_dir = Path(secrets_dir)
    if not legacy_dir.is_dir():
        return 0
    try:
        if legacy_dir.resolve() == secrets_dir.resolve():
            return 0
    except OSError:
        return 0
    migrated = 0
    for name in ('ai_key.bin', 'image_key.bin'):
        try:
            if _migrate_file(legacy_dir / name, secrets_dir / name):
                migrated += 1
        except OSError:
            continue
    old_keys = legacy_dir / 'image_keys'
    if old_keys.is_dir():
        for entry in sorted(old_keys.glob('*.bin')):
            try:
                if _migrate_file(entry, secrets_dir / 'image_keys' / entry.name):
                    migrated += 1
            except OSError:
                continue
        try:
            if not any(old_keys.iterdir()):
                old_keys.rmdir()
        except OSError:
            pass
    try:
        if not any(legacy_dir.iterdir()):
            legacy_dir.rmdir()
    except OSError:
        pass
    return migrated


def redact(value, key):
    return value.replace(key, '***') if key else value


class AISettings:
    defaults = dict(engine='codex', base_url='', model='', reasoning_effort='medium')

    def __init__(self, root, key_path=None):
        self.root = Path(root).resolve()
        self.path = self.root/'ai_settings.json'
        self.key_path = Path(key_path) if key_path is not None else None
        self.lock = threading.RLock()
        if self.key_path is None and os.name == 'nt' and _is_app_root(self.root):
            migrate_legacy_secrets(self.root / 'secrets')

    def _key_file(self):
        if self.key_path is not None:
            return self.key_path
        if os.name != 'nt':
            raise AIError('仅支持 Windows 存储密钥')
        return self.root / 'secrets' / 'ai_key.bin'

    def _read(self):
        from .core import read_json

        try:
            saved = read_json(self.path)
        except FileNotFoundError:
            saved = {}
        return {**self.defaults, **saved}

    def _read_key(self):
        if self.key_path is None and os.name != 'nt':
            return ''
        try:
            encrypted = self._key_file().read_bytes()
        except FileNotFoundError:
            return ''
        except OSError:
            raise AIError('无法读取已保存的 API Key') from None
        return dpapi(encrypted, decrypt=True).decode('utf-8')

    @staticmethod
    def _fields(saved, values):
        fields = {name: values.get(name, saved[name]) for name in AISettings.defaults}
        fields['base_url'] = normalize_url(fields['base_url'])
        if fields['engine'] not in ('codex', 'openai_api'):
            raise AIError('校准引擎无效')
        if fields['reasoning_effort'] not in (None, 'low', 'medium', 'high'):
            raise AIError('推理强度无效')
        model = fields['model']
        if not isinstance(model, str) or any(ord(c) < 32 for c in model):
            raise AIError('模型名无效')
        fields['model'] = model.strip()
        return fields

    @staticmethod
    def _capability(saved, fields):
        return saved.get('capabilities', {}).get(fields['base_url'], {}).get(fields['model'], {})

    def _public(self, saved, key):
        return {**{name: redact(saved[name], key) if isinstance(saved[name], str) and name in ('model', 'base_url')
                   else saved[name] for name in self.defaults},
                'key_set': bool(key), 'key_tail': key[-4:] if len(key) > 4 else '',
                'structured_mode': self._capability(saved, saved).get('structured_mode')}

    def public(self):
        with self.lock:
            return self._public(self._read(), self._read_key())

    def save(self, values):
        from .core import atomic_json

        with self.lock:
            saved = self._read()
            fields = self._fields(saved, values)
            key = values.get('api_key', '')
            if isinstance(key, str) and key.strip().lower().startswith('bearer '):
                key = key.strip()[7:]  # 粘贴时多带了 "Bearer " 前缀
            if not isinstance(key, str) or any(not 33 <= ord(c) <= 126 for c in key.strip()):
                raise AIError('API Key 格式无效')
            key = key.strip()
            clear = values.get('clear_key') is True
            current = self._read_key() if not clear and not key else ''
            active = '' if clear else key or current
            self._check_key(fields, active)
            updated = {**fields, 'capabilities': saved.get('capabilities', {})}
            if key and not clear:
                try:
                    target = self._key_file()
                    encrypted = dpapi(key.encode('utf-8'))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temp = target.with_name(f'{uuid.uuid4().hex}.tmp')
                    temp.write_bytes(encrypted)
                    os.replace(temp, target)
                except OSError:
                    raise AIError('无法保存 API Key') from None
            elif clear:
                try:
                    self._key_file().unlink(missing_ok=True)
                except OSError:
                    raise AIError('无法清除 API Key') from None
            atomic_json(self.path, updated)
            return self._public(updated, active)

    @staticmethod
    def _check_key(fields, key):
        if key and any(key in fields[name] for name in ('base_url', 'model')):
            raise AIError('接口地址和模型名中不能包含 API Key')

    def engine(self):
        with self.lock:
            return self._read()['engine']

    def connection(self, values=None):
        with self.lock:
            saved = self._read()
            fields = self._fields(saved, values or {})
            key = self._read_key()
            if (isinstance(values, dict) and 'base_url' in values and saved.get('base_url')
                    and url_origin(fields['base_url']) != url_origin(saved['base_url'])):
                key = ''
            self._check_key(fields, key)
            return fields, key, self._capability(saved, fields)

    def remember(self, fields, **capability):
        from .core import atomic_json

        with self.lock:
            saved = self._read()
            entry = saved.setdefault('capabilities', {}).setdefault(fields['base_url'], {}).setdefault(fields['model'], {})
            if any(entry.get(name) != value for name, value in capability.items()):
                entry.update(capability)
                atomic_json(self.path, saved)
