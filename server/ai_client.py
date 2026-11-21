"""OpenAI 兼容中转站的图片校准、模型列表和连接测试。"""
from __future__ import annotations

import base64
from importlib import import_module
import io
import json
from pathlib import Path
import platform
import re
import struct
import sys
import time

from PIL import Image, ImageOps

from . import PYLIBS
from .ai_settings import AIError, redact


TIMEOUT = 180
BACKOFF = (2, 6)
MODES = ('json_schema', 'json_object', 'text')
TOKEN_FIELDS = ('prompt_tokens', 'completion_tokens', 'total_tokens')
INVALID_RESULT = '模型没有按要求返回结果'
MISSING_SDK = '缺少内置 OpenAI SDK（pylibs 文件夹），请完整复制工作台文件夹'
PROBE_RGB = (200, 60, 40)
PROBE_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['description', 'red', 'green', 'blue'],
    'properties': {'description': {'type': 'string'},
                   **{name: {'type': 'integer', 'minimum': 0, 'maximum': 255}
                      for name in ('red', 'green', 'blue')}},
}


class RelayError(AIError):
    def __init__(self, message, *, status=None, format_related=False, effort_related=False):
        super().__init__(message)
        self.status = status
        self.format_related = format_related
        self.effort_related = effort_related


def load_sdk():
    if getattr(sys, 'frozen', False):
        # 安装版：SDK 已由 PyInstaller 打进程序本体，直接导入
        try:
            return import_module('openai')
        except Exception:
            raise AIError(MISSING_SDK) from None
    sdk_file = PYLIBS / 'openai' / '__init__.py'
    try:
        available = sdk_file.is_file()
    except OSError:
        available = False
    if not available:
        raise AIError(MISSING_SDK)
    try:
        sdk = import_module('openai')
        if not getattr(sdk, '__file__', None) or Path(sdk.__file__).resolve() != sdk_file:
            raise ImportError
        return sdk
    except Exception:
        # 导入第三方模块时，二进制依赖初始化也可能抛出异常。
        bits = struct.calcsize('P') * 8
        if sys.version_info[:2] != (3, 13) or bits != 64:
            raise AIError(f'中转站功能需要 Python 3.13（64 位），当前是 Python {platform.python_version()}（{bits} 位）') from None
        raise AIError(MISSING_SDK) from None


def http_error(status, detail):
    detail = re.sub(r'[-_\s]+', ' ', detail.lower())
    if status == 403 and ('1010' in detail or 'cloudflare' in detail or 'access denied' in detail):
        message = '中转站的防护（Cloudflare）拦截了请求（HTTP 403），不是 Key 的问题'
    elif status == 401:
        message = 'API Key 无效或没有权限'
    elif status == 403:
        message = 'API Key 没有权限，或中转站拒绝了请求（HTTP 403）'
    elif status == 404 or 'model not found' in detail:
        message = '中转站没有这个模型'
    elif status == 429:
        message = '中转站限流或额度不足，请稍后再试'
    elif status in (408, 504):
        message = '中转站响应超时'
    elif 300 <= status < 400:
        message = '中转站要求跳转，请填写最终接口地址'
    elif status >= 500:
        message = '中转站暂时不可用，请稍后再试'
    else:
        message = f'中转站拒绝请求（HTTP {status}）'
    can_fallback = status == 400 and message != '中转站没有这个模型'
    return RelayError(message, status=status,
                      format_related=can_fallback and any(term in detail for term in (
                          'response format', 'json schema', 'json object')),
                      effort_related=can_fallback and 'reasoning effort' in detail)


def image_url(source, *, preserve_png=False):
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original)
        # 分区图缩放时只取原色，避免插值产生不存在的材质编号。
        image.thumbnail((1600, 1600), Image.Resampling.NEAREST if preserve_png else Image.Resampling.LANCZOS)
        stream = io.BytesIO()
        if preserve_png:
            image.save(stream, 'PNG')
            mime = 'image/png'
        else:
            image.convert('RGB').save(stream, 'JPEG', quality=92)
            mime = 'image/jpeg'
    return f'data:{mime};base64,' + base64.b64encode(stream.getvalue()).decode('ascii')


def redact_output(value, key):
    if isinstance(value, dict):
        return {redact(name, key): redact_output(entry, key) for name, entry in value.items()}
    if isinstance(value, list):
        return [redact_output(entry, key) for entry in value]
    return redact(value, key) if isinstance(value, str) else value


class OpenAIClient:
    def __init__(self, settings, values=None):
        self.settings = settings
        self.fields, self.key, self.capability = settings.connection(values)
        self.base_url = self.fields['base_url']
        self.model = self.fields['model']
        self.mode = None
        self.connected = False
        self.tokens = {}

    def _request(self, sdk, payload=None):
        if not self.base_url:
            raise AIError('请填写接口地址')
        if not self.key:
            raise AIError('请先保存 API Key')
        # Bearer 凭据只发送到设置中的地址。
        with sdk.OpenAI(api_key=self.key, base_url=self.base_url, timeout=TIMEOUT, max_retries=0,
                        http_client=sdk.DefaultHttpxClient(follow_redirects=False)) as client:
            for attempt in range(len(BACKOFF)+1):
                try:
                    response = client.models.list() if payload is None else client.chat.completions.create(**payload)
                    self.connected = True
                    return response.model_dump(warnings=False) if hasattr(response, 'model_dump') else response
                except sdk.APIStatusError as cause:
                    detail = cause.response.text[:65536]
                    try:
                        detail = json.dumps(json.loads(detail), ensure_ascii=False)
                    except ValueError:
                        pass
                    error = http_error(cause.status_code, redact(detail, self.key))
                    if cause.status_code != 429 and cause.status_code < 500:
                        raise error from None
                except sdk.APITimeoutError:
                    error = RelayError('中转站响应超时')
                except sdk.APIConnectionError:
                    error = RelayError('连不上中转站，请检查地址和网络')
                except (sdk.APIResponseValidationError, ValueError):
                    self.connected = True
                    raise RelayError(INVALID_RESULT) from None
                if attempt == len(BACKOFF):
                    raise error from None
                time.sleep(BACKOFF[attempt])

    def models(self):
        response = self._request(load_sdk())
        try:
            if not isinstance(response['data'], list):
                raise TypeError
            models = {entry['id'] for entry in response['data'] if isinstance(entry, dict)
                      and isinstance(entry.get('id'), str) and entry['id'] and self.key not in entry['id']}
        except (KeyError, TypeError):
            raise RelayError('中转站没有返回有效的模型列表') from None
        return sorted(models, key=lambda model: (model != 'gpt-6.2-sol', model.casefold(), model))

    def _record_tokens(self, response):
        usage = response.get('usage') if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            return
        for name in TOKEN_FIELDS:
            value = usage.get(name)
            if type(value) is int and value >= 0:
                self.tokens[name] = self.tokens.get(name, 0)+value

    def analyze(self, images, system, prompt, schema, validate, *, schema_name='calibration'):
        sdk = load_sdk()
        if not self.model:
            raise AIError('请选择或填写模型')
        mode = self.capability.get('structured_mode')
        index = MODES.index(mode) if mode in MODES else 0
        effort = None if self.capability.get('omit_effort') else self.fields['reasoning_effort']
        image_parts = [{'type': 'image_url', 'image_url': {'url': url}} for url in images]
        retries = 0
        while True:
            self.mode = mode = MODES[index]
            text = prompt
            if mode == 'text':
                text += '\n只输出符合以下 schema 的 JSON。\n'+json.dumps(schema, ensure_ascii=False)
            elif mode == 'json_object':
                text += '\n只输出 JSON，符合以下 schema。\n'+json.dumps(schema, ensure_ascii=False)
            request = {'model': self.model, 'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': [{'type': 'text', 'text': text}, *image_parts]}]}
            if mode == 'json_schema':
                request['response_format'] = {'type': mode, 'json_schema': {
                    'name': schema_name, 'schema': schema, 'strict': True}}
            elif mode == 'json_object':
                request['response_format'] = {'type': mode}
            if effort is not None:
                request['reasoning_effort'] = effort
            try:
                response = self._request(sdk, request)
            except RelayError as error:
                if error.effort_related and effort is not None:
                    effort = None
                    self.settings.remember(self.fields, omit_effort=True)
                    continue
                if error.format_related and index < len(MODES)-1:
                    index += 1
                    continue
                if str(error) != INVALID_RESULT or retries:
                    raise
                retries += 1
                continue
            self._record_tokens(response)
            try:
                content = response['choices'][0]['message']['content'].strip()
                fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', content, re.IGNORECASE | re.DOTALL)
                output = json.loads(fenced[1] if fenced else content)
                output = redact_output(output, self.key)
                validate(output, schema)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
                if retries:
                    raise RelayError(INVALID_RESULT) from None
                retries += 1
                continue
            self.settings.remember(self.fields, structured_mode=mode)
            return output

    def usage(self, elapsed):
        return {'engine': 'openai_api', 'model': self.model, 'latency_sec': round(elapsed, 2), **self.tokens}


def round_images(images, descriptions):
    return [image_url(path, preserve_png=Path(path).suffix.lower() == '.png' and '材质分区图' in description['role'])
            for path, description in zip(images, descriptions, strict=True)]


def test_connection(settings, validate, values=None):
    start = time.monotonic()
    client = OpenAIClient(settings, values)
    result = {'connected': False, 'vision': False, 'structured_mode': None, 'model': client.model}
    stream = io.BytesIO()
    Image.new('RGB', (64, 64), PROBE_RGB).save(stream, 'PNG')
    stream.seek(0)
    try:
        value = client.analyze([image_url(stream, preserve_png=True)], '你负责识别图片颜色。',
                               '用简体中文简述图片的主色，并给出大致的红、绿、蓝数值。只输出 JSON。',
                               PROBE_SCHEMA, validate, schema_name='color_probe')
        result['vision'] = bool(value['description'].strip()) and all(
            abs(value[name]-expected) <= 35 for name, expected in zip(('red', 'green', 'blue'), PROBE_RGB))
        result['structured_mode'] = client.mode
        if not result['vision']:
            result['reason'] = '识别出的颜色与测试图偏差较大'
    except AIError as error:
        result['reason'] = str(error)
    return dict(result, connected=client.connected, latency_sec=round(time.monotonic()-start, 2))
