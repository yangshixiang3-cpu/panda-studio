"""校准引擎设置与当前 Windows 用户的 DPAPI 密钥。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
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


def normalize_url(value):
    if not isinstance(value, str):
        raise AIError('请输入 http:// 或 https:// 接口地址')
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
        raise AIError('请输入有效的 http:// 或 https:// 接口地址') from None
    return value


def redact(value, key):
    return value.replace(key, '***') if key else value


class AISettings:
    defaults = dict(engine='codex', base_url='', model='', reasoning_effort='medium')

    def __init__(self, root, key_path=None):
        self.path = Path(root)/'ai_settings.json'
        self.key_path = Path(key_path) if key_path is not None else None
        self.lock = threading.RLock()

    def _key_file(self):
        if self.key_path is not None:
            return self.key_path
        if os.name != 'nt':
            raise AIError('仅支持 Windows 存储密钥')
        appdata = os.environ.get('APPDATA')
        if not appdata:
            raise AIError('找不到 Windows 用户的应用数据目录')
        return Path(appdata)/'whitebg-studio'/'ai_key.bin'

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
