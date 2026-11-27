"""画布生图：连接 OpenAI 兼容的生图接口。

两种接口都支持（「接口类型」选自动时按模型名判断）：
- 图片接口：/v1/images/generations（没有参考图）和 /v1/images/edits（有参考图），如 gpt-image 系列；
- 对话接口：/v1/chat/completions 让模型直接返回图片（Markdown 里的 data URL / 图片链接，或 message.images），
  如 Gemini 生图、4o-image 等中转站常见的写法。
设置按「渠道」存（image_api.json）：一个渠道 = 接口地址 + Key + 模型 + 接口类型（+ 拉到的模型列表），
可以存好几个、改名，生图时选用哪个。每个渠道的 API Key 用 Windows DPAPI 加密，存在
<程序目录>\\secrets\\image_keys\\<渠道号>.bin；也可以沿用「AI 设置」里中转站的 Key（默认）。
1.5.2 以前只有一套设置（image_key.bin），第一次读取时自动变成「渠道 1」。
"""
from __future__ import annotations

import base64
import io
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from .ai_settings import AIError, AISettings, _is_app_root, dpapi, migrate_legacy_secrets, normalize_url, redact, url_origin

TIMEOUT = 300
# gpt-image / dall-e 只认这几种尺寸；其他模型按「尺寸档位 + 比例」算出宽高（64 的倍数）
RATIOS = {'1:1': '1024x1024', '3:4': '1024x1536', '2:3': '1024x1536', '9:16': '1024x1536',
          '4:3': '1536x1024', '3:2': '1536x1024', '16:9': '1536x1024'}
RESOLUTIONS = {'1K': 1024, '2K': 2048, '4K': 4096}
QUALITIES = ('standard', 'high')
FIXED_SIZE_RE = re.compile(r'gpt-image|dall-e', re.I)
# 比例和清晰度怎么传给图片接口（每个渠道自己选，不同中转站、不同模型认的字段不一样）：
#   auto：gpt-image / dall-e 用它们的标准尺寸；其他模型 size（像素）、aspect_ratio、ratio、resolution 全带上（同一个比例，不冲突）
#   size：只传像素尺寸；aspect_ratio / ratio：只传这个比例字段 + resolution；all：全带上
SIZE_STYLES = ('auto', 'size', 'aspect_ratio', 'ratio', 'all')
RESERVED_EXTRA = ('model', 'prompt', 'image', 'images', 'n', 'contents')
MAX_EXTRA = 4000
GEMINI_RE = re.compile(r'gemini|banana|nano', re.I)
# 异步生图任务（FluxPort 中转站文档 中转站接口文档：较重的请求回 202 + object=image.task，查 status_url，图在 assets 里）
TASK_STATES = ('queued', 'dispatching', 'running', 'pending', 'processing', 'submitted', 'in_progress', 'created', 'starting')
TASK_DONE = ('success', 'succeeded', 'completed', 'complete', 'done', 'finished')
TASK_WAIT = 1200           # 一个异步任务最多等 20 分钟（文档建议本地上限 30 分钟）
TASK_BACKOFF = (5, 10, 20, 40, 60)      # 查询遇到 429、5xx、断网时的重试间隔（秒），最多 5 次
IMAGE_FIELDS = ('b64_json', 'b64', 'base64', 'image_base64', 'signed_url', 'download_url', 'url', 'image_url', 'imageUrl',
                'image', 'content_url', 'file_url', 'public_url', 'uri', 'src', 'fileUri', 'file_uri')
MODES = ('auto', 'images', 'chat', 'gemini')      # gemini：Gemini 原生 generateContent
MAX_COUNT = 4
MAX_REFS = 8
MAX_PROMPT = 4000
CHANNEL_RE = re.compile(r'^[0-9a-f]{8}$')
MAX_CHANNELS = 30
MAX_MODELS = 300
CHAT_MODEL_RE = re.compile(r'gemini|banana|nano|4o-image|image-preview|flash-image|chat', re.I)
DATA_URL_RE = re.compile(r'data:image/(?:png|jpe?g|webp|gif);base64,[A-Za-z0-9+/=\s]+')
HTTP_IMAGE_RE = re.compile(r'https?://[^\s)\]"\'<>]+', re.I)
MAX_IMAGE_BYTES = 40 * 1024 * 1024


def _clean_text(value, limit, what):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise AIError(f'{what}无效')
    return value.strip()


class ImageAPISettings:
    """生图渠道。渠道没填接口地址时沿用 AI 设置里中转站的地址；没填 Key 且勾了沿用时用中转站的 Key。"""
    fields = dict(name='', base_url='', model='', mode='auto', reuse_ai_key=True, models=[], size_style='auto', extra={})

    def __init__(self, root, key_path=None, ai_settings=None):
        self.root = Path(root).resolve()
        self.path = self.root / 'image_api.json'
        self.key_path = Path(key_path) if key_path is not None else None
        self.ai = ai_settings or AISettings(self.root)
        self.lock = threading.RLock()
        if self.key_path is None and os.name == 'nt' and _is_app_root(self.root):
            migrate_legacy_secrets(self.root / 'secrets')

    # ── 密钥文件 ──
    def _key_file(self):
        """1.5.2 以前唯一的那个 Key 文件；渠道的 Key 放在它旁边的 image_keys 文件夹里。"""
        if self.key_path is not None:
            return self.key_path
        if os.name != 'nt':
            raise AIError('仅支持 Windows 存储密钥')
        return self.root / 'secrets' / 'image_key.bin'

    def _channel_key_file(self, cid):
        if not CHANNEL_RE.fullmatch(str(cid)):
            raise AIError('渠道号无效')
        return self._key_file().parent / 'image_keys' / f'{cid}.bin'

    def _own_key(self, cid):
        try:
            encrypted = self._channel_key_file(cid).read_bytes()
        except FileNotFoundError:
            return ''
        except OSError:
            raise AIError('无法读取已保存的生图 API Key') from None
        return dpapi(encrypted, decrypt=True).decode('utf-8')

    def _write_key(self, cid, key):
        target = self._channel_key_file(cid)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(uuid.uuid4().hex + '.tmp')
        temp.write_bytes(dpapi(key.encode('utf-8')))
        os.replace(temp, target)

    # ── 读写 ──
    def _clean_channel(self, raw):
        channel = {**self.fields, **{k: v for k, v in raw.items() if k in self.fields}, 'id': raw['id']}
        channel['mode'] = channel['mode'] if channel['mode'] in MODES else 'auto'
        channel['reuse_ai_key'] = channel['reuse_ai_key'] is not False
        channel['size_style'] = channel['size_style'] if channel['size_style'] in SIZE_STYLES else 'auto'
        channel['extra'] = channel['extra'] if isinstance(channel['extra'], dict) else {}
        channel['models'] = [m for m in channel['models'] if isinstance(m, str)][:MAX_MODELS] if isinstance(channel['models'], list) else []
        for name in ('name', 'base_url', 'model'):
            channel[name] = str(channel[name] or '')
        return channel

    def _read(self):
        """返回 {'active': 渠道号或 '', 'channels': [...]}；旧版单套设置在这里变成「渠道 1」。"""
        from .core import atomic_json, read_json
        try:
            saved = read_json(self.path)
        except (FileNotFoundError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        if isinstance(saved.get('channels'), list):
            channels = [self._clean_channel(c) for c in saved['channels']
                        if isinstance(c, dict) and CHANNEL_RE.fullmatch(str(c.get('id', '')))]
            active = saved.get('active') if any(c['id'] == saved.get('active') for c in channels) else ''
            return {'active': active or (channels[0]['id'] if channels else ''), 'channels': channels}
        # 旧版：只有一套设置
        old_key = self._key_file()
        if not saved and not old_key.is_file():
            return {'active': '', 'channels': []}
        cid = uuid.uuid4().hex[:8]
        channel = self._clean_channel({**saved, 'id': cid, 'name': '渠道 1'})
        if old_key.is_file():
            target = self._channel_key_file(cid)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_key, target)
        data = {'active': cid, 'channels': [channel]}
        atomic_json(self.path, {'version': 2, **data})
        return data

    def _write(self, data):
        from .core import atomic_json
        atomic_json(self.path, {'version': 2, 'active': data['active'], 'channels': data['channels']})

    def _find(self, data, cid):
        for channel in data['channels']:
            if channel['id'] == cid:
                return channel
        raise AIError('没有这个生图渠道（可能已删除），请重新选择。')

    def _ai(self):
        try:
            fields, key, _ = self.ai.connection()
            return fields.get('base_url', ''), key
        except AIError:
            return '', ''

    def connection(self, values=None, channel=None):
        """返回（接口地址, Key, 模型, 接口类型）。
        channel：渠道号，不给就用当前渠道；values：设置页里还没保存的内容（拉模型用，可带 id 指明是哪个渠道）。"""
        blank = {**self.fields, 'id': ''}
        with self.lock:
            data = self._read()
            cid = channel or (values or {}).get('id')
            if cid:
                saved = self._find(data, cid)
            elif values is not None:
                saved = blank                        # 设置页里新建、还没保存的渠道：不拿别的渠道的 Key
            else:
                saved = next((c for c in data['channels'] if c['id'] == data['active']), blank)
            values = values if isinstance(values, dict) else {}
            merged = {**saved, **{k: v for k, v in values.items() if k in ('base_url', 'model', 'mode', 'reuse_ai_key')}}
            ai_url, ai_key = self._ai()
            saved_url = normalize_url(str(saved['base_url'] or '') or ai_url)
            base_url = normalize_url(str(merged['base_url'] or '') or ai_url)
            typed = values.get('api_key', '')
            typed = typed.strip() if isinstance(typed, str) else ''
            if typed.lower().startswith('bearer '):
                typed = typed[7:].strip()
            unsaved_url = 'base_url' in values
            same_saved_origin = (not saved_url) or url_origin(base_url) == url_origin(saved_url)
            same_ai_origin = bool(ai_url) and url_origin(base_url) == url_origin(ai_url)
            if typed:
                key = typed
            elif saved['id'] and (not unsaved_url or same_saved_origin):
                key = self._own_key(saved['id'])
            else:
                key = ''
            if not key and merged['reuse_ai_key'] is not False and (not unsaved_url or same_ai_origin):
                key = ai_key
            mode = merged['mode'] if merged['mode'] in MODES else 'auto'
            return base_url, key, str(merged['model'] or '').strip(), mode

    def options(self, channel=None):
        """渠道的比例传法和额外参数（生成时用）。"""
        with self.lock:
            data = self._read()
            cid = channel or data['active']
            found = next((c for c in data['channels'] if c['id'] == cid), None)
            return {'size_style': found['size_style'], 'extra': found['extra']} if found else {'size_style': 'auto', 'extra': {}}

    def public(self):
        with self.lock:
            data = self._read()
            ai_url, ai_key = self._ai()
            channels = []
            for channel in data['channels']:
                own = self._own_key(channel['id'])
                source = 'own' if own else 'ai' if channel['reuse_ai_key'] and ai_key else 'none'
                key = own or (ai_key if source == 'ai' else '')
                channels.append({**channel, 'key_source': source, 'key_tail': key[-4:] if len(key) > 4 else ''})
            current = next((c for c in channels if c['id'] == data['active']), None)
            return {'active': data['active'], 'channels': channels, 'ai_base_url': ai_url,
                    # 兼容：当前渠道的模型
                    'model': current['model'] if current else ''}

    def _next_name(self, channels):
        numbers = [int(m.group(1)) for c in channels for m in [re.fullmatch(r'渠道 (\d+)', c['name'])] if m]
        return f'渠道 {max(numbers, default=0) + 1}'

    def save(self, values):
        """新建（不带 id）或修改一个渠道，改完的渠道号放在返回值的 saved 里。新建的渠道成为当前渠道。"""
        if not isinstance(values, dict):
            raise AIError('设置格式无效')
        with self.lock:
            data = self._read()
            cid = values.get('id')
            if cid:
                channel = self._find(data, cid)
            else:
                if len(data['channels']) >= MAX_CHANNELS:
                    raise AIError(f'渠道最多 {MAX_CHANNELS} 个')
                channel = self._clean_channel({'id': uuid.uuid4().hex[:8]})
                channel['name'] = self._next_name(data['channels'])
            updated = dict(channel)
            if 'name' in values:
                updated['name'] = _clean_text(values['name'], 40, '渠道名')[:40] or channel['name'] or self._next_name(data['channels'])
            if 'base_url' in values:
                updated['base_url'] = normalize_url(_clean_text(values['base_url'] or '', 500, '接口地址'))
            if 'model' in values:
                updated['model'] = _clean_text(values['model'] or '', 200, '模型名')
            if 'mode' in values:
                if values['mode'] not in MODES:
                    raise AIError('接口类型无效')
                updated['mode'] = values['mode']
            if 'reuse_ai_key' in values:
                updated['reuse_ai_key'] = values['reuse_ai_key'] is True
            if 'size_style' in values:
                if values['size_style'] not in SIZE_STYLES:
                    raise AIError('比例的传法无效')
                updated['size_style'] = values['size_style']
            if 'extra' in values:
                updated['extra'] = clean_extra(values['extra'])
            if 'models' in values:
                models = values['models']
                if not isinstance(models, list):
                    raise AIError('模型列表格式无效')
                updated['models'] = [m.strip() for m in models if isinstance(m, str) and m.strip()
                                     and len(m) <= 200 and not any(ord(c) < 32 for c in m)][:MAX_MODELS]
            key = values.get('api_key', '')
            if not isinstance(key, str):
                raise AIError('API Key 格式无效')
            key = key.strip()
            if key.lower().startswith('bearer '):
                key = key[7:].strip()
            if key and any(not 33 <= ord(c) <= 126 for c in key):
                raise AIError('API Key 格式无效')
            if key and (key in updated['base_url'] or key in updated['model'] or key in updated['name']):
                raise AIError('渠道名、接口地址和模型名中不能包含 API Key')
            if values.get('clear_key') is True:
                self._channel_key_file(updated['id']).unlink(missing_ok=True)
            elif key:
                self._write_key(updated['id'], key)
            if cid:
                data['channels'] = [updated if c['id'] == cid else c for c in data['channels']]
            else:
                data['channels'].append(updated)
                data['active'] = updated['id']
            if not data['active']:
                data['active'] = updated['id']
            self._write(data)
        return {**self.public(), 'saved': updated['id']}

    def activate(self, cid):
        with self.lock:
            data = self._read()
            self._find(data, cid)
            data['active'] = cid
            self._write(data)
        return self.public()

    def copy(self, cid):
        """复制一个渠道（连 Key 一起），方便同一个站点、同一个 Key 再配一个模型。"""
        with self.lock:
            data = self._read()
            source = self._find(data, cid)
            if len(data['channels']) >= MAX_CHANNELS:
                raise AIError(f'渠道最多 {MAX_CHANNELS} 个')
            channel = {**source, 'id': uuid.uuid4().hex[:8], 'name': (source['name'] + ' 副本')[:40]}
            own = self._own_key(cid)
            if own:
                self._write_key(channel['id'], own)
            data['channels'].insert(data['channels'].index(source) + 1, channel)
            data['active'] = channel['id']
            self._write(data)
        return {**self.public(), 'saved': channel['id']}

    def delete(self, cid):
        with self.lock:
            data = self._read()
            self._find(data, cid)
            data['channels'] = [c for c in data['channels'] if c['id'] != cid]
            if data['active'] == cid:
                data['active'] = data['channels'][0]['id'] if data['channels'] else ''
            self._write(data)
            self._channel_key_file(cid).unlink(missing_ok=True)
        return self.public()


def image_size(model, ratio, resolution):
    """像素尺寸：gpt-image / dall-e 只有固定几种；其他按「长边 1024、2048、4096 + 比例」算，取 64 的倍数。"""
    if FIXED_SIZE_RE.search(model or ''):
        return RATIOS[ratio]
    w, h = (int(v) for v in ratio.split(':'))
    long_side = RESOLUTIONS.get(resolution, 2048)
    if w >= h:
        return f'{long_side}x{max(64, round(long_side * h / w / 64) * 64)}'
    return f'{max(64, round(long_side * w / h / 64) * 64)}x{long_side}'


def size_params(model, ratio, resolution, style='auto'):
    """按渠道选的传法，返回（放进请求的 size 或 None, 其他字段）。"""
    if style == 'auto':
        style = 'size' if FIXED_SIZE_RE.search(model or '') else 'all'
    size = image_size(model, ratio, resolution)
    if style == 'size':
        return size, {}
    if style in ('aspect_ratio', 'ratio'):
        return None, {style: ratio, 'resolution': resolution}
    return size, {'aspect_ratio': ratio, 'ratio': ratio, 'resolution': resolution}


def ratio_hint(ratio):
    """写进提示词的比例说明。"""
    w, h = (int(v) for v in ratio.split(':'))
    shape = '横版' if w > h else '竖版' if w < h else '方图'
    return f'画面比例 {ratio}（{shape}）。'


def fit_ratio(content, extension, ratio):
    """出图后对比例（写死，不用每个渠道去试）：
    横竖方向对、只差一点的（裁掉不到四分之一），居中裁到要的比例；方向反了或差太多不裁，返回一句说明。
    返回（字节, 扩展名, 说明）。"""
    w_r, h_r = (int(v) for v in ratio.split(':'))
    want = w_r / h_r
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            w, h = image.size
            got = w / h
            if abs(got - want) / want < 0.012:
                return content, extension, ''
            if (got > 1.02 and want < 0.98) or (got < 0.98 and want > 1.02):
                return content, extension, f'接口出的是 {w}x{h}，和要的 {ratio} 横竖方向不一样，没有裁（这家中转站可能不认比例）'
            nw, nh = (round(h * want), h) if got > want else (w, round(w / want))
            if nw * nh < w * h * 0.75:
                return content, extension, f'接口出的是 {w}x{h}，和要的 {ratio} 差得多，没有裁'
            left, top = (w - nw) // 2, (h - nh) // 2
            cropped = image.crop((left, top, left + nw, top + nh))
            stream = io.BytesIO()
            if extension in ('jpg', 'jpeg'):
                cropped.convert('RGB').save(stream, 'JPEG', quality=95)
                extension = 'jpg'
            elif extension == 'webp':
                cropped.save(stream, 'WEBP', quality=95)
            else:
                cropped.save(stream, 'PNG')
                extension = 'png'
            return stream.getvalue(), extension, ''
    except Exception:            # 看不懂的图不动它
        return content, extension, ''


def clean_extra(value):
    """渠道的额外参数：JSON 对象（可以是文字），原样并进每次请求；不能改 model、prompt、参考图这些。"""
    if value in (None, ''):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise AIError('额外参数要是 JSON，比如 {"watermark": false}') from None
    if not isinstance(value, dict):
        raise AIError('额外参数要是 JSON 对象，比如 {"watermark": false}')
    if len(json.dumps(value, ensure_ascii=False)) > MAX_EXTRA:
        raise AIError('额外参数太长')
    taken = [k for k in value if k in RESERVED_EXTRA]
    if taken:
        raise AIError(f'额外参数里不能有 {"、".join(taken)}（软件自己会填）')
    return value


def _merge(base, extra):
    """把额外参数并进请求：字典里的字典逐层合并，其余直接覆盖。"""
    merged = dict(base)
    for k, v in extra.items():
        merged[k] = _merge(merged[k], v) if isinstance(v, dict) and isinstance(merged.get(k), dict) else v
    return merged


def quality_param(model, quality):
    """高清画质：gpt-image 传 high，dall-e 传 hd；其他模型不传（靠尺寸档位）。"""
    if quality != 'high':
        return None
    if re.search(r'dall-e', model or '', re.I):
        return 'hd'
    if re.search(r'gpt-image', model or '', re.I):
        return 'high'
    return None


def resolve_mode(mode, model):
    if mode in ('images', 'chat', 'gemini'):
        return mode
    return 'chat' if CHAT_MODEL_RE.search(model or '') else 'images'


def _image_bytes(data):
    """校验是图片，返回（字节, 扩展名）。"""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise AIError('接口返回的图片无效')
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
            kind = (image.format or '').lower()
    except Exception:
        raise AIError('接口返回的不是图片') from None
    return data, {'jpeg': 'jpg'}.get(kind, kind if kind in ('png', 'webp', 'gif', 'bmp') else 'png')


AUTH_HEADERS = frozenset({'authorization', 'x-goog-api-key', 'cookie'})


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _forbidden_ip(ip):
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified
            or ip.is_multicast or ip.is_reserved)


def _validate_download_url(url, *, allow_origin=None, allow_loopback=False):
    """校验下载链接：仅允许 http/https，解析主机并拒绝本机/内网/链路本地/未指定/组播地址（与生图接口同源的回环除外）。"""
    parsed = urlsplit(str(url or '').strip())
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname:
        raise AIError('不允许下载非 http/https 地址的图片')
    if parsed.username is not None or parsed.password is not None:
        raise AIError('图片地址不能包含账号信息')
    same_origin = bool(allow_origin) and url_origin(url) == url_origin(allow_origin)
    host = parsed.hostname
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        port = parsed.port or (443 if parsed.scheme.lower() == 'https' else 80)
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise AIError('无法解析图片地址的主机名') from None
        ips = [ipaddress.ip_address(info[4][0].split('%', 1)[0]) for info in infos]
    if not ips:
        raise AIError('无法解析图片地址的主机名')
    for ip in ips:
        check = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None else ip
        if check.is_loopback and (same_origin or allow_loopback):
            continue
        if _forbidden_ip(ip):
            raise AIError('禁止从本机或内网地址下载图片')


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """图片下载允许公开跳转，但每次跳转都剥离认证头并重新校验目标地址（防 Key 泄露与跳转 SSRF）。"""

    def __init__(self, *, allow_origin=None, allow_loopback=False):
        self.allow_origin = allow_origin
        self.allow_loopback = allow_loopback

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        full = urljoin(req.full_url, newurl)
        _validate_download_url(full, allow_origin=self.allow_origin, allow_loopback=self.allow_loopback)
        redirected = super().redirect_request(req, fp, code, msg, headers, full)
        if redirected is not None:
            for bag in (redirected.headers, redirected.unredirected_hdrs):
                for name in list(bag):
                    if name.lower() in AUTH_HEADERS:
                        del bag[name]
        return redirected


def _download(url, *, allow_origin=None, allow_loopback=False):
    _validate_download_url(url, allow_origin=allow_origin, allow_loopback=allow_loopback)
    request = urllib.request.Request(url, headers={'User-Agent': 'PandaStudio'})
    opener = urllib.request.build_opener(_SafeRedirectHandler(allow_origin=allow_origin, allow_loopback=allow_loopback))
    with opener.open(request, timeout=120) as response:   # noqa: S310 已校验协议与目标 IP
        return response.read(MAX_IMAGE_BYTES + 1)


def _decode_data_url(text):
    return base64.b64decode(re.sub(r'\s+', '', text.split(',', 1)[1]))


class ImageGenerator:
    def __init__(self, settings):
        self.settings = settings

    def _client(self, sdk, base_url, key):
        if not base_url:
            raise AIError('请先在「生图设置」里填写接口地址')
        if not key:
            raise AIError('请先在「生图设置」里填写 API Key')
        return sdk.OpenAI(api_key=key, base_url=base_url, timeout=TIMEOUT, max_retries=0,
                          http_client=sdk.DefaultHttpxClient(follow_redirects=False))

    def _call(self, sdk, key, work):
        from .ai_client import RelayError, http_error
        try:
            return work()
        except sdk.APIStatusError as cause:
            detail = redact(cause.response.text[:4000], key)
            error = http_error(cause.status_code, detail)
            hint = ''
            try:
                message = json.loads(detail).get('error', {})
                hint = message.get('message', '') if isinstance(message, dict) else str(message)
            except (ValueError, AttributeError):
                pass
            if re.search(r'images? api.{0,40}not (be )?support', detail, re.I | re.S):
                # 如中转站的 Gemini：「Images API is not supported for this platform」，要走对话接口
                failure = RelayError('这个模型不支持图片接口（/images），要在生图渠道里把接口类型改成「Gemini 原生」或「对话接口」',
                                     status=cause.status_code)
                failure.images_unsupported = True
                raise failure from None
            raise RelayError(f'{error}{"：" + hint[:200] if hint else ""}', status=cause.status_code) from None
        except sdk.APITimeoutError:
            raise RelayError('生图接口响应超时') from None
        except sdk.APIConnectionError:
            raise RelayError('连不上生图接口，请检查地址和网络') from None

    def models(self, values=None):
        from .ai_client import load_sdk
        sdk = load_sdk()
        base_url, key, _, _ = self.settings.connection(values)
        with self._client(sdk, base_url, key) as client:
            response = self._call(sdk, key, lambda: client.models.list())
        names = sorted({m.id for m in response.data if isinstance(getattr(m, 'id', None), str) and key not in m.id})
        image_like = [n for n in names if re.search(r'image|dall|banana|imagen|flux|seedream|kolors|cogview|wanx|jimeng|mj|midjourney', n, re.I)]
        return {'models': image_like + [n for n in names if n not in image_like], 'image_models': image_like}

    def generate(self, prompt, refs, ratio, count, quality='standard', resolution='2K', channel=None):
        """refs：参考图文件路径列表；channel：用哪个渠道（不给就用当前渠道）。返回 [(字节, 扩展名)]。"""
        from .ai_client import load_sdk
        prompt = str(prompt or '').strip()
        if not prompt:
            raise AIError('请先写提示词')
        if len(prompt) > MAX_PROMPT:
            raise AIError('提示词太长')
        if ratio not in RATIOS:
            raise AIError('画面比例无效')
        quality = quality if quality in QUALITIES else 'standard'
        resolution = resolution if resolution in RESOLUTIONS else '2K'
        count = max(1, min(MAX_COUNT, int(count or 1)))
        refs = list(refs or [])[:MAX_REFS]
        sdk = load_sdk()
        base_url, key, model, mode = self.settings.connection(channel=channel)
        if not model:
            raise AIError('请先在「生图设置」里选择模型')
        mode = resolve_mode(mode, model)
        opts = self.settings.options(channel) if hasattr(self.settings, 'options') else {'size_style': 'auto', 'extra': {}}
        with self._client(sdk, base_url, key) as client:
            single = {'images': self._images_once, 'gemini': self._gemini_once}.get(mode, self._chat_once)
            results, errors = self._batch(single, count, sdk, client, key, model, prompt, refs, ratio, quality, resolution, opts)
            if not results and mode == 'images' and getattr(errors[0], 'images_unsupported', False):
                # 图片接口被拒、说这个模型不支持：Gemini 类改走原生接口，其余改走对话接口（被拒的请求不出图，也不会扣费）
                fallback = self._gemini_once if GEMINI_RE.search(model) else self._chat_once
                results, errors = self._batch(fallback, count, sdk, client, key, model, prompt, refs, ratio, quality,
                                              resolution, opts)
        if not results:
            raise errors[0]
        return results

    def _batch(self, single, count, *args):
        """一次出 count 张：每张单独请求，同时发。返回（成功的结果, 失败的错误）。"""
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(single, *args) for _ in range(count)]
            results, errors = [], []
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as error:
                    errors.append(error)
        return results, errors

    def _images_once(self, sdk, client, key, model, prompt, refs, ratio, quality, resolution, opts=None):
        opts = opts or {}
        size, body = size_params(model, ratio, resolution, opts.get('size_style', 'auto'))
        params = {'model': model, 'prompt': f'{prompt}\n\n{ratio_hint(ratio)}', 'n': 1}
        if quality_param(model, quality):
            params['quality'] = quality_param(model, quality)
        if size:
            params['size'] = size
        body = _merge(body, opts.get('extra') or {})
        headers = {'Idempotency-Key': 'panda-' + uuid.uuid4().hex}      # 同一次提交不会被中转站重复生成
        if refs:
            files = [(Path(p).name, Path(p).read_bytes(), 'image/png' if Path(p).suffix.lower() == '.png' else 'image/jpeg')
                     for p in refs]
            # 参考图用列表发（字段 image[]，即梦等要这样）；dall-e 只认一张 image
            image = files[0] if len(files) == 1 and re.search(r'dall-e', model, re.I) else files
            raw = self._call(sdk, key, lambda: client.images.with_raw_response.edit(
                image=image, extra_body=body or None, extra_headers=headers, **params))
        else:
            raw = self._call(sdk, key, lambda: client.images.with_raw_response.generate(
                extra_body=body or None, extra_headers=headers, **params))
        # 不用 SDK 解析好的结果：中转站的写法五花八门（还可能是 202 异步任务），认不出时要把原话带给用户看
        return self._finish(raw.text, client.base_url, key)

    def _finish(self, text, base_url, key):
        """解析提交后的返回：同步结果直接取图；异步任务先等它结束再取。"""
        try:
            payload = json.loads(text)
        except ValueError:
            raise AIError('生图接口返回的不是 JSON：' + _snippet(text, key)) from None
        fetch = _fetcher(base_url, key)
        if is_image_task(payload):
            payload = self._wait_task(payload, fetch, key)
        return _image_bytes(extract_images_image(payload, key, fetch))

    def _gemini_once(self, sdk, client, key, model, prompt, refs, ratio, quality, resolution, opts=None):
        """Gemini 原生接口：POST {站点}/v1beta/models/{模型}:generateContent，画幅和分辨率放在 imageConfig。"""
        from urllib.parse import quote
        parts = [{'text': prompt}]
        for path in refs:
            data = Path(path).read_bytes()
            mime = 'image/png' if Path(path).suffix.lower() == '.png' else 'image/webp' if Path(path).suffix.lower() == '.webp' \
                else 'image/jpeg'
            parts.append({'inlineData': {'mimeType': mime, 'data': base64.b64encode(data).decode('ascii')}})
        body = {'contents': [{'role': 'user', 'parts': parts}],
                'generationConfig': {'responseModalities': ['TEXT', 'IMAGE'],
                                     'imageConfig': {'aspectRatio': ratio, 'imageSize': resolution}}}
        body = _merge(body, (opts or {}).get('extra') or {})
        url = f'{api_root(client.base_url)}/v1beta/models/{quote(model, safe="")}:generateContent'
        text = _post_json(url, body, key)
        return self._finish(text, client.base_url, key)

    def _wait_task(self, task, fetch, key):
        """异步任务（按 FluxPort 文档）：按 poll_after_ms 查 status_url，直到 success 或停下来的状态；
        success 后用 assets，没有 assets 再读 result_url 的原结果。只查生图接口同一个站点上的地址。"""
        task_id = str(task.get('task_id') or task.get('id') or '')
        tasks = f'images/tasks/{task_id}' if task_id else ''
        status_path = task.get('status_url') or task.get('poll_url') or task.get('result_url') or (tasks and tasks + '?view=summary')
        result_path = task.get('result_url') or task.get('poll_url') or tasks
        if not status_path:
            raise AIError('生图接口给了异步任务，但没给查询地址。接口返回：' + _snippet(task, key))
        deadline, failures = time.monotonic() + TASK_WAIT, 0
        while True:
            state = str(task.get('status') or '').lower()
            if state in TASK_DONE or task.get('assets'):
                break
            # 失败、未确认、暂停、取消、未知状态，或中转站说别再查：停下，由 extract_images_image 说明原因
            if state not in TASK_STATES or task.get('auto_poll') is False or task.get('poll_after_ms') == 0:
                return task
            if time.monotonic() > deadline:
                raise AIError(f'等了 {TASK_WAIT // 60} 分钟还没出图，先停止等待（任务没取消，任务号 {task_id}，可以拿去问中转站）')
            delay = task.get('poll_after_ms')
            time.sleep(min(10, max(2, (delay if isinstance(delay, (int, float)) and delay > 0 else 2000) / 1000)))
            try:
                text = fetch(status_path, json_reply=True)
            except FetchError as error:
                failures += 1
                if not error.retryable or failures > len(TASK_BACKOFF):
                    raise
                time.sleep(TASK_BACKOFF[failures - 1])
                continue
            failures = 0
            task = _json_object(text, key, '查询生图任务')
        if task.get('assets') or task.get('data') or task.get('candidates') or not result_path:
            return task
        result = _json_object(fetch(result_path, json_reply=True), key, '读取生图结果')
        return {**result, 'task_id': result.get('task_id') or task_id}

    def _chat_once(self, sdk, client, key, model, prompt, refs, ratio, quality, resolution, opts=None):
        from .ai_client import image_url
        detail = '，高清、细节丰富' if quality == 'high' else ''
        text = f'{prompt}\n\n请直接生成一张图片，画面比例 {ratio}，分辨率约 {resolution}{detail}。'
        content = [{'type': 'text', 'text': text}] + [{'type': 'image_url', 'image_url': {'url': image_url(p)}}
                                                      for p in refs]
        extra = (opts or {}).get('extra') or None
        response = self._call(sdk, key, lambda: client.chat.completions.create(
            model=model, messages=[{'role': 'user', 'content': content}], extra_body=extra))
        payload = response.model_dump(warnings=False) if hasattr(response, 'model_dump') else response
        return _image_bytes(extract_chat_image(payload, key, allow_origin=url_origin(client.base_url)))


def _snippet(value, key='', limit=300):
    """接口原话的摘要：长的 base64 截掉、去掉 Key、最多 limit 个字。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = re.sub(r'[A-Za-z0-9+/=]{200,}', '…', text)
    text = redact(text, key) if key else text
    text = re.sub(r'\s+', ' ', text).strip()
    return (text[:limit] + '…') if len(text) > limit else text or '（空）'


def _error_text(error):
    if isinstance(error, dict):
        return str(error.get('message') or error.get('msg') or error.get('code') or json.dumps(error, ensure_ascii=False))
    return str(error)


class FetchError(AIError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def api_root(base_url):
    """接口根地址：https://x.com/v1 → https://x.com（Gemini 原生接口在 /v1beta 下）。"""
    base = str(base_url).rstrip('/')
    return base[:-3] if base.endswith('/v1') else base


def _json_object(text, key, what):
    try:
        data = json.loads(text)
    except ValueError:
        raise AIError(f'{what}时接口返回的不是 JSON：{_snippet(text, key, 150)}') from None
    if not isinstance(data, dict):
        raise AIError(f'{what}时接口返回的格式不对：{_snippet(data, key, 150)}')
    return data


def _post_json(url, body, key):
    """发 JSON（Gemini 原生接口用），返回原文；拒绝重定向以防泄露 Key。HTTP 错误按中转站错误说明。"""
    from .ai_client import RelayError, http_error
    request = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'), method='POST', headers={
        'Authorization': 'Bearer ' + key, 'x-goog-api-key': key, 'Content-Type': 'application/json',
        'Idempotency-Key': 'panda-' + uuid.uuid4().hex, 'User-Agent': 'PandaStudio'})
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=TIMEOUT) as response:   # noqa: S310 用户设置的生图接口，禁止跳转
            return response.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as error:
        detail = redact(error.read(4000).decode('utf-8', 'replace'), key)
        hint = ''
        try:
            message = json.loads(detail).get('error', {})
            hint = message.get('message', '') if isinstance(message, dict) else str(message)
        except (ValueError, AttributeError):
            pass
        raise RelayError(f'{http_error(error.code, detail)}{"：" + hint[:200] if hint else ""}', status=error.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RelayError('连不上生图接口，请检查地址和网络') from None


def _fetcher(base_url, key):
    """下载接口给的地址：相对地址按生图接口的站点补全；同一个站点带上 Key，别的站点不带（不把 Key 发给别人）。"""
    base = str(base_url).rstrip('/') + '/'
    parsed_base = urlsplit(base)
    origin = f'{parsed_base.scheme}://{parsed_base.netloc}'
    norm_origin = url_origin(base)

    def fetch(url, json_reply=False):
        url = str(url).strip()
        full = origin + url if url.startswith('/') else url if re.match(r'https?://', url, re.I) else urljoin(base, url)
        same = url_origin(full) == norm_origin
        if json_reply and not same:
            raise FetchError('生图任务的查询地址不在生图接口的站点上，不去查')
        if not json_reply:
            try:
                _validate_download_url(full, allow_origin=norm_origin)
            except AIError as error:
                raise FetchError(str(error), retryable=False) from None
        headers = {'User-Agent': 'PandaStudio', **({'Authorization': 'Bearer ' + key} if same and key else {})}
        request = urllib.request.Request(full, headers=headers)
        opener = (_NO_REDIRECT_OPENER if json_reply
                  else urllib.request.build_opener(_SafeRedirectHandler(allow_origin=norm_origin)))
        try:
            with opener.open(request, timeout=120) as response:   # noqa: S310 已禁止带 Key 跳转并校验 SSRF
                content = response.read(MAX_IMAGE_BYTES + 1)
        except AIError as error:
            raise FetchError(str(error), retryable=False) from None
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                raise FetchError('生图接口要求跳转，已拒绝跟随', retryable=False) from None
            detail = error.read(2000).decode('utf-8', 'replace')
            raise FetchError(f'{"查询生图任务" if json_reply else "下载图片"}失败（HTTP {error.code}）：{_snippet(detail, key, 150)}',
                             retryable=error.code == 429 or error.code >= 500) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise FetchError('查询生图任务时连不上接口' if json_reply else '下载生成的图片失败，请检查网络', retryable=True) from None
        return content.decode('utf-8', 'replace') if json_reply else content
    return fetch


def is_image_task(payload):
    """异步生图任务（还没给图，只给了任务号）。"""
    if not isinstance(payload, dict) or payload.get('data'):
        return False
    state = str(payload.get('status') or '').lower()
    return payload.get('object') == 'image.task' or bool(payload.get('poll_url')) or \
        (bool(payload.get('task_id')) and state in TASK_STATES)


def _image_from(value, fetch=None):
    """一个字段里的图：data URL、链接（相对地址交给 fetch）或纯 base64。认不出返回 None。"""
    if isinstance(value, dict):
        value = next((value[k] for k in IMAGE_FIELDS if isinstance(value.get(k), str) and value[k].strip()), None)
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.startswith('data:image/'):
        return _decode_data_url(value)
    if re.match(r'https?://', value, re.I):
        return fetch(value) if fetch else _download(value)
    if value.startswith('/') and fetch and len(value) < 2000:
        return fetch(value)
    if len(value) > 100 and re.fullmatch(r'[A-Za-z0-9+/=\s]+', value):
        return base64.b64decode(re.sub(r'\s+', '', value))
    return None


def extract_images_image(payload, key='', fetch=None):
    """从图片接口（/images）的返回里找第一张图。
    标准写法是 data[].b64_json 或 data[].url；也认中转站常见的 b64、image_url、images[]、data 套一层等写法。
    即梦（火山方舟）某张图失败时 data 里是 {error: {code, message}}，把原因带出来；
    返回任务号（异步接口）时说明暂不支持；都认不出就把接口原话放进错误里。"""
    if not isinstance(payload, dict):
        raise AIError('生图接口没有返回图片。接口返回：' + _snippet(payload, key))
    if isinstance(payload.get('candidates'), list):
        return _gemini_image(payload, key, fetch)
    entries = payload.get('data')
    if isinstance(entries, dict):
        entries = entries.get('data') or entries.get('images') or [entries]
    entries = list(entries) if isinstance(entries, list) else []
    for name in ('images', 'assets'):
        if isinstance(payload.get(name), list):
            entries += payload[name]
    entries.append({k: payload[k] for k in ('url', 'b64_json', 'image', 'image_url') if k in payload})
    errors = []
    for entry in entries:
        if not isinstance(entry, dict):
            entry = {'image': entry}
        for name in IMAGE_FIELDS:
            found = _image_from(entry.get(name), fetch)
            if found:
                return found
        if entry.get('error'):
            errors.append(_error_text(entry['error']))
    if errors:
        raise AIError('生图失败：' + _snippet(errors[0], key))
    task_id = str(payload.get('task_id') or (payload.get('id') if payload.get('object') == 'image.task' else '') or '')
    tail = f'（任务号 {task_id}，可以拿去问中转站）' if task_id else ''
    state = str(payload.get('status') or '').lower()
    billed = any(isinstance(payload.get(k), (int, float)) and payload[k] > 0 for k in ('billed_amount', 'charged_amount'))
    unbilled = all(isinstance(payload.get(k), (int, float)) for k in ('billed_amount', 'charged_amount')) and not billed
    fee = '（没扣费）' if unbilled else ''
    if state == 'uncertain':
        reason = (payload.get('safe_error') or {}).get('reason_code') if isinstance(payload.get('safe_error'), dict) else ''
        if reason == 'service_temporarily_unavailable' or '暂时不可用' in str(payload.get('error') or ''):
            raise AIError(f'中转站那边的生成服务暂时不可用，这次没出图{fee}。中转站让先别重复提交，可以过一会儿再试，或换个模型{tail}')
        reason = _error_text(payload.get('error') or payload.get('status_message') or '结果还没确认')
        raise AIError(f'中转站说这次结果还没确认{fee}：{_snippet(reason, key, 200)}{tail}')
    if state == 'client_disconnected':
        raise AIError(f'和中转站的连接断了，这次结果还没确认{fee}，先别重复提交{tail}')
    if state == 'paused':
        raise AIError(f'中转站把这次任务暂停了，要在中转站恢复后才会继续{tail}')
    if state in ('canceled', 'cancelled'):
        raise AIError(f'这次任务被取消了{fee}{tail}')
    if payload.get('error'):
        raise AIError('生图失败：' + _snippet(_error_text(payload['error']), key, 250) + tail)
    if payload.get('status_message') and state not in TASK_DONE:
        raise AIError(f'生图失败（{state or "未知状态"}）：{_snippet(payload["status_message"], key, 250)}{tail}')
    if state in TASK_STATES:
        raise AIError(f'中转站让先停止查询，任务还没完成，可以过一会儿再看{tail}')
    if payload.get('task_id'):
        raise AIError('生图任务结束了，但没拿到图片。接口返回：' + _snippet(payload, key))
    raise AIError('生图接口没有返回图片。接口返回：' + _snippet(payload, key))


def _gemini_image(payload, key='', fetch=None):
    """Gemini 原生结果：candidates[].content.parts[] 里的 inlineData（或 fileData.fileUri）。"""
    texts, reasons = [], []
    for candidate in payload.get('candidates') or []:
        if not isinstance(candidate, dict):
            continue
        if candidate.get('finishReason') not in (None, 'STOP'):
            reasons.append(str(candidate['finishReason']))
        for part in ((candidate.get('content') or {}).get('parts') or []):
            if not isinstance(part, dict):
                continue
            inline = part.get('inlineData') or part.get('inline_data')
            if isinstance(inline, dict) and inline.get('data'):
                return base64.b64decode(re.sub(r'\s+', '', inline['data']))
            file = part.get('fileData') or part.get('file_data')
            if isinstance(file, dict):
                found = _image_from(file.get('fileUri') or file.get('file_uri'), fetch)
                if found:
                    return found
            if part.get('text'):
                texts.append(str(part['text']))
    blocked = (payload.get('promptFeedback') or {}).get('blockReason') if isinstance(payload.get('promptFeedback'), dict) else None
    if blocked:
        raise AIError(f'提示词被拦截了（{blocked}），换个说法再试')
    detail = f'（{"、".join(reasons)}）' if reasons else ''
    reply = f'。模型回复：{_snippet(" ".join(texts), key, 120)}' if texts else ''
    raise AIError(f'模型没有返回图片{detail}{reply}')


def extract_chat_image(payload, key='', *, allow_origin=None):
    """从对话接口的返回里找第一张图：message.images（OpenRouter 写法）、内容里的 data URL 或图片链接。"""
    try:
        message = payload['choices'][0]['message']
    except (KeyError, IndexError, TypeError):
        raise AIError('生图接口没有返回内容。接口返回：' + _snippet(payload, key)) from None
    for entry in message.get('images') or []:
        url = (entry.get('image_url') or {}).get('url') if isinstance(entry, dict) else None
        if isinstance(url, str):
            return _decode_data_url(url) if url.startswith('data:') else _download(url, allow_origin=allow_origin)
    content = message.get('content')
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get('type') == 'image_url' and isinstance((part.get('image_url') or {}).get('url'), str):
                    url = part['image_url']['url']
                    return _decode_data_url(url) if url.startswith('data:') else _download(url, allow_origin=allow_origin)
                parts.append(str(part.get('text') or ''))
        content = '\n'.join(parts)
    content = str(content or '')
    found = DATA_URL_RE.search(content)
    if found:
        return _decode_data_url(found.group(0))
    for url in HTTP_IMAGE_RE.findall(content):
        url = url.rstrip('.,;')
        if re.search(r'\.(png|jpe?g|webp|gif)(\?|$)', url, re.I) or 'image' in url.lower():
            return _download(url, allow_origin=allow_origin)
    reply = f'。模型回复：{_snippet(content, key, 120)}' if content.strip() else ''
    raise AIError(f'模型没有返回图片（可能这个模型不支持生图，或要在生图渠道里换成图片接口）{reply}')
