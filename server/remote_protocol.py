"""内网渲染机：工作台（本机）和渲染机之间共用的约定。

作业在两边的写法：
- 本机交换目录里的路径写成 "@ex:tasks/<任务号>/…"（相对交换目录，正斜杠），渲染机换成它自己工作区里的路径；
- 产品 .max 写成 "@model:<相对模型包的路径>"，渲染机换成它解开的模型包里的文件；
- 白底模板、渲染预设由渲染机用自己的，本机传 None。
模型包 = 产品文件夹里出图要用的文件（.max 和贴图等），打成 zip 传过去；同样内容只传一次（按文件清单算的指纹）。
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
import uuid
import zipfile
from pathlib import Path, PurePosixPath

SERVICE = 'panda-studio-node'
PROTOCOL = 1
NODE_PORT = 8775
DISCOVERY_PORT = 8776
DISCOVER_MAGIC = b'PANDA-STUDIO-DISCOVER/1'
EX_PREFIX = '@ex:'
MODEL_PREFIX = '@model:'

JOB_ID_RE = re.compile(r'^\d{8}-\d{6}-[0-9a-f]{4}$')
BUNDLE_ID_RE = re.compile(r'^[0-9a-f]{64}$')
CLIENT_KEY_RE = re.compile(r'^[0-9a-f]{8}$')

# 模型包：跳过的文件夹（出图结果、参考照片、备份等）和文件（别的模型、压缩包、备份）
BUNDLE_SKIP_DIRS = {'sku', '视角图', '参考图', '旧版', 'autoback', 'exchange', 'preview', 'smallshots', 'final',
                    '_extra', 'node_modules', '.git', '__pycache__'}
BUNDLE_SKIP_SUFFIXES = {'.max', '.fbx', '.obj', '.3ds', '.dwg', '.skp', '.zip', '.rar', '.7z', '.bak', '.tmp', '.prev',
                        '.lnk'}
BUNDLE_MAX_BYTES = 3 * 1024 ** 3
BUNDLE_MAX_FILES = 20000
# 渲染机解包时的上限（防止异常压缩包撑爆磁盘）
EXTRACT_MAX_BYTES = 8 * 1024 ** 3
EXTRACT_MAX_FILES = 50000


class ProtocolError(ValueError):
    """作业或压缩包不符合约定。"""


def safe_rel(value):
    """检查相对路径：正斜杠、不能是绝对路径、不能有 .. 和空段，不能带盘符或 Windows 保留字符。返回规范化后的字符串。"""
    if not isinstance(value, str) or not value or len(value) > 1000:
        raise ProtocolError('相对路径无效')
    if '\\' in value or ':' in value or value.startswith('/') or '\x00' in value:
        raise ProtocolError('相对路径无效：' + value)
    parts = PurePosixPath(value).parts
    if not parts or any(part in ('', '.', '..') or re.search(r'[<>"|?*\x00-\x1f]', part) for part in parts):
        raise ProtocolError('相对路径无效：' + value)
    return '/'.join(parts)


def inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


# ── 模型包 ──────────────────────────────────────────────

def bundle_base(max_file, product_dir=None):
    """模型包从哪个文件夹打：.max 在产品文件夹里就打整个产品文件夹（贴图可能放在别的子文件夹），否则打 .max 所在文件夹。"""
    max_file = Path(max_file).resolve()
    if product_dir:
        product = Path(product_dir).resolve()
        if inside(max_file, product):
            return product
    return max_file.parent


def bundle_files(max_file, product_dir=None):
    """列出模型包要带的文件：[(相对路径, 绝对路径, 大小, 修改时间纳秒)]，选中的 .max 一定在内。"""
    max_file = Path(max_file).resolve()
    if not max_file.is_file():
        raise ProtocolError('找不到产品模型：' + str(max_file))
    base = bundle_base(max_file, product_dir)
    files, total = [], 0
    for directory, folders, names in os.walk(base):
        folders[:] = sorted(f for f in folders if f.casefold() not in BUNDLE_SKIP_DIRS and not f.startswith('.'))
        for name in sorted(names):
            path = Path(directory) / name
            if path != max_file and (path.suffix.casefold() in BUNDLE_SKIP_SUFFIXES or name.startswith('~')):
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            rel = path.relative_to(base).as_posix()
            files.append((rel, path, info.st_size, info.st_mtime_ns))
            total += info.st_size
            if len(files) > BUNDLE_MAX_FILES:
                raise ProtocolError(f'产品文件夹里的文件太多（超过 {BUNDLE_MAX_FILES} 个），没法传到渲染机。')
            if total > BUNDLE_MAX_BYTES:
                raise ProtocolError('产品文件夹太大（超过 3 GB），没法传到渲染机。')
    return base, max_file.relative_to(base).as_posix(), files


def bundle_id(max_rel, files):
    digest = hashlib.sha256(('max\t' + max_rel + '\n').encode('utf-8'))
    for rel, _, size, mtime in sorted(files, key=lambda f: f[0]):
        digest.update(f'{rel}\t{size}\t{mtime}\n'.encode('utf-8'))
    return digest.hexdigest()


def write_bundle(files, target):
    """把文件打成 zip（快速压缩）。写到临时名再改名。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.' + uuid.uuid4().hex[:8] + '.part')
    try:
        with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            for rel, path, _, _ in files:
                archive.write(path, rel)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return target


def extract_bundle(archive_path, target):
    """渲染机：安全解开模型包到 target（先解到临时文件夹，检查完再改名）。target 已存在就直接用。"""
    target = Path(target)
    if target.is_dir():
        return target
    temp = target.with_name(target.name + '.' + uuid.uuid4().hex[:8] + '.part')
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) > EXTRACT_MAX_FILES:
                raise ProtocolError('模型包文件太多。')
            if sum(e.file_size for e in entries) > EXTRACT_MAX_BYTES:
                raise ProtocolError('模型包太大。')
            for entry in entries:
                if entry.is_dir():
                    continue
                rel = safe_rel(entry.filename)
                destination = temp / rel
                if not inside(destination, temp):
                    raise ProtocolError('模型包路径越界。')
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, destination.open('wb') as stream:
                    shutil.copyfileobj(source, stream, 1024 * 1024)
        try:
            os.replace(temp, target)
        except OSError:
            if not target.is_dir():
                raise
    except zipfile.BadZipFile:
        raise ProtocolError('模型包不是有效的 zip。') from None
    finally:
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)
    return target


# ── 作业路径换算 ────────────────────────────────────────

def _map_strings(value, convert):
    if isinstance(value, dict):
        return {k: _map_strings(v, convert) for k, v in value.items()}
    if isinstance(value, list):
        return [_map_strings(v, convert) for v in value]
    if isinstance(value, str):
        return convert(value)
    return value


def portable_job(job, exchange, base, max_rel):
    """本机作业 → 发给渲染机的写法。exchange：本机交换目录；base：模型包根目录；max_rel：.max 在包里的相对路径。"""
    exchange = Path(exchange).resolve()
    result = copy.deepcopy(job)
    result['max_file'] = MODEL_PREFIX + max_rel
    result['template_max'] = None
    if 'render_preset_rps' in result:
        result['render_preset_rps'] = None

    def convert(text):
        if len(text) < 4 or not (text[1:3] in (':\\', ':/') or text.startswith('\\\\')):
            return text
        path = Path(text)
        if path.is_absolute() and inside(path, exchange):
            return EX_PREFIX + path.resolve().relative_to(exchange).as_posix()
        return text

    for key in [k for k in result if k not in ('max_file', 'template_max', 'render_preset_rps')]:
        result[key] = _map_strings(result[key], convert)
    return result


def node_job(portable, *, node_exchange, node_job_id, node_task_id, bundle_dir, template, preset=None):
    """渲染机：发来的作业 → 写进自己交换目录的作业。返回（作业, 需要先建好的文件夹列表）。"""
    if not isinstance(portable, dict):
        raise ProtocolError('作业格式无效。')
    job = copy.deepcopy(portable)
    if not JOB_ID_RE.fullmatch(str(job.get('job_id', ''))) or not JOB_ID_RE.fullmatch(str(job.get('task_id', ''))):
        raise ProtocolError('作业号或任务号无效。')
    if job.get('type') not in ('export_preview', 'render'):
        raise ProtocolError('作业类型无效。')
    if job['type'] == 'render' and (job.get('mode') not in ('smallshot', 'final') or not isinstance(job.get('items'), list)):
        raise ProtocolError('渲染作业格式无效。')
    task_id = job['task_id']
    node_exchange = Path(node_exchange).resolve()
    workspace = node_exchange / 'tasks' / node_task_id
    prefix = f'tasks/{task_id}/'
    folders = set()

    def convert(text):
        if text.startswith(EX_PREFIX):
            rel = safe_rel(text[len(EX_PREFIX):])
            if not rel.startswith(prefix):
                raise ProtocolError('作业路径不在本任务里：' + rel)
            path = workspace / rel[len(prefix):]
            folders.add(path.parent if PurePosixPath(rel).suffix else path)
            return str(path)
        if text.startswith(MODEL_PREFIX):
            raise ProtocolError('模型路径只能出现在 max_file。')
        return text

    model = job.get('max_file')
    if not isinstance(model, str) or not model.startswith(MODEL_PREFIX):
        raise ProtocolError('作业缺少模型。')
    model_path = Path(bundle_dir) / safe_rel(model[len(MODEL_PREFIX):])
    if not inside(model_path, bundle_dir) or not model_path.is_file():
        raise ProtocolError('模型包里没有这个 .max。')
    rest = {k: _map_strings(v, convert) for k, v in job.items() if k != 'max_file'}
    _reject_outside(rest, workspace)
    job = {**rest, 'job_id': node_job_id, 'task_id': node_task_id, 'max_file': str(model_path),
           'template_max': str(template), 'asset_dirs': [str(Path(bundle_dir))]}
    if 'render_preset_rps' in job or preset:
        job['render_preset_rps'] = str(preset) if preset else None
    folders.add(workspace)
    return job, sorted(folders)


ABSOLUTE_RE = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\|//)')


def _reject_outside(value, workspace):
    """换算后作业里只能出现渲染机工作区里的绝对路径：发来的作业不能让执行器读写这台电脑别处的文件。"""
    if isinstance(value, dict):
        for item in value.values():
            _reject_outside(item, workspace)
    elif isinstance(value, list):
        for item in value:
            _reject_outside(item, workspace)
    elif isinstance(value, str) and ABSOLUTE_RE.match(value) and not inside(value, workspace):
        raise ProtocolError('作业里有不允许的路径：' + value)


def portable_status(status, workspace, task_id):
    """渲染机状态里的输出路径（渲染机工作区）→ "@ex:tasks/<本机任务号>/…"。"""
    workspace = Path(workspace).resolve()
    result = copy.deepcopy(status)

    def convert(text):
        if len(text) > 3 and text[1:3] in (':\\', ':/'):
            path = Path(text)
            if inside(path, workspace):
                return EX_PREFIX + f'tasks/{task_id}/' + path.resolve().relative_to(workspace).as_posix()
        return text

    for item in result.get('items') or []:
        for key in ('out_image', 'out_mask'):
            if isinstance(item.get(key), str):
                item[key] = convert(item[key])
    return result


def local_path(value, exchange, task_id):
    """本机：把 "@ex:tasks/<任务号>/…" 换回本机交换目录里的绝对路径（只允许落在这个任务的文件夹里）。"""
    if not isinstance(value, str) or not value.startswith(EX_PREFIX):
        return value
    rel = safe_rel(value[len(EX_PREFIX):])
    if not rel.startswith(f'tasks/{task_id}/'):
        raise ProtocolError('渲染机返回的路径不在本任务里。')
    exchange = Path(exchange).resolve()
    path = exchange / rel
    if not inside(path, exchange / 'tasks' / task_id):
        raise ProtocolError('渲染机返回的路径越界。')
    return str(path)
