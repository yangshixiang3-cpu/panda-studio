"""画布：项目、多张画布、图片文件、生图任务、记住的设置。

数据都在交换目录的 canvas/ 里：
- projects/<项目号>/project.json：项目名、画布列表（顶部的画布标签）；
- projects/<项目号>/canvases/<画布号>.json：一张画布的内容（节点、连线、分组、视角），网页自动保存；
- projects/<项目号>/images/：这个项目拖进来和生成的图片（「项目管理」里点「打开文件夹」看的就是这里）；
- runs/<任务号>.json：每次生成的状态和结果；网页关掉再打开也能接上；
- prefs.json：上次用的生图设置（比例、画质、尺寸、张数）、自己存的提示词预设、上次打开的画布；
- trash/：删掉的项目和画布（不直接删文件）。
1.5.x 的单张画布（canvas/board.json + canvas/files/）第一次打开时迁移成「我的项目 / 画布 1」。
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from .ai_settings import AIError
from .image_gen import CHANNEL_RE, MAX_COUNT, fit_ratio, MAX_REFS, QUALITIES, RATIOS, RESOLUTIONS, ImageAPISettings, ImageGenerator

PROJECT_RE = re.compile(r'^[0-9a-f]{12}$')
CANVAS_RE = re.compile(r'^[0-9a-f]{8}$')
FILE_RE = re.compile(r'^[0-9a-f]{32}\.(png|jpg|webp|gif|bmp)$')
THUMB_SIZES = (256, 1280)   # 缩略图两档：小图（参考图栏、@ 列表、项目卡片）和节点
RUN_RE = re.compile(r'^[0-9a-f]{32}$')
RUN_KEEP_DAYS = 30            # 生成记录保留天数
MAX_BOARD_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_NODES = 1000
MAX_EDGES = 4000
MAX_CANVASES = 50
MAX_PRESETS = 60
UPLOAD_FORMATS = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp', 'GIF': 'gif', 'BMP': 'bmp'}
CONCURRENT_RUNS = 4
DEFAULT_GEN = {'ratio': '16:9', 'quality': 'standard', 'resolution': '2K', 'count': 1}
EMPTY_BOARD = {'version': 2, 'view': None, 'nodes': [], 'edges': [], 'groups': [], 'seq': 0}


class CanvasError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _read(path):
    from .core import read_json
    return read_json(path)


def _write(path, data):
    from .core import atomic_json
    atomic_json(path, data)


def _now():
    from .core import now
    return now()


def clean_name(value, fallback):
    text = re.sub(r'[\x00-\x1f\\/:*?"<>|]', '', str(value or '')).strip()
    return text[:40] or fallback


class CanvasStore:
    def __init__(self, studio, settings=None, generator=None):
        self.studio = studio
        self.root = studio.exchange / 'canvas'
        self.projects = self.root / 'projects'
        self.runs = self.root / 'runs'
        self.trash = self.root / 'trash'
        self._ensure_dirs()
        self.settings = settings or ImageAPISettings(studio.root, ai_settings=studio.calibration.ai_settings)
        self.generator = generator or ImageGenerator(self.settings)
        self.lock = threading.RLock()
        self.slots = threading.Semaphore(CONCURRENT_RUNS)
        self._migrate()
        self._recover()

    # ── 项目 ──
    def _project_dir(self, pid):
        if not isinstance(pid, str) or not PROJECT_RE.fullmatch(pid):
            raise CanvasError('项目号无效。', 404)
        return self.projects / pid

    def project(self, pid):
        path = self._project_dir(pid) / 'project.json'
        if not path.is_file():
            raise CanvasError('没有这个项目（可能已删除）。', 404)
        return _read(path)

    def _save_project(self, project):
        project['updated'] = _now()
        _write(self._project_dir(project['id']) / 'project.json', project)
        return project

    def _summary(self, project):
        images = self._project_dir(project['id']) / 'images'
        files = sorted((p for p in images.glob('*') if FILE_RE.fullmatch(p.name)), key=lambda p: p.stat().st_mtime,
                       reverse=True) if images.is_dir() else []
        return {**project, 'image_count': len(files), 'thumbs': [p.name for p in files[:4]],
                'folder': str(self._project_dir(project['id']))}

    def _ensure_dirs(self):
        # 软件开着时用户可能在资源管理器里删掉了画布文件夹：用到时再建回来
        for folder in (self.projects, self.runs, self.trash):
            folder.mkdir(parents=True, exist_ok=True)

    def list_projects(self):
        self._ensure_dirs()
        found = []
        for folder in self.projects.iterdir():
            if PROJECT_RE.fullmatch(folder.name) and (folder / 'project.json').is_file():
                try:
                    found.append(self._summary(_read(folder / 'project.json')))
                except (OSError, ValueError):
                    continue
        if not found:
            found.append(self._summary(self.create_project('我的项目')))
        found.sort(key=lambda p: p.get('updated', ''), reverse=True)
        return {'projects': found, 'root': str(self.projects), 'prefs': self.prefs()}

    def create_project(self, name=None):
        with self.lock:
            pid, cid = uuid.uuid4().hex[:12], uuid.uuid4().hex[:8]
            folder = self._project_dir(pid)
            (folder / 'images').mkdir(parents=True)
            (folder / 'canvases').mkdir()
            project = {'id': pid, 'name': clean_name(name, '未命名项目'), 'created': _now(), 'updated': _now(),
                       'canvases': [{'id': cid, 'name': '画布 1'}]}
            return self._save_project(project)

    def rename_project(self, pid, name):
        with self.lock:
            project = self.project(pid)
            project['name'] = clean_name(name, project['name'])
            return self._save_project(project)

    def delete_project(self, pid):
        with self.lock:
            project = self.project(pid)
            self._ensure_dirs()
            target = self.trash / f'{pid}-{int(time.time())}'
            os.replace(self._project_dir(pid), target)
            return {'deleted': pid, 'name': project['name']}

    def restore_project(self, pid):
        with self.lock:
            candidates = sorted((p for p in self.trash.glob(f'{self._project_dir(pid).name}-*') if p.is_dir()), reverse=True)
            if not candidates or self._project_dir(pid).exists():
                raise CanvasError('回收站里没有这个项目。', 404)
            os.replace(candidates[0], self._project_dir(pid))
            return self.project(pid)

    # ── 画布（顶部标签） ──
    def _canvas_path(self, pid, cid, project=None):
        project = project or self.project(pid)
        if not isinstance(cid, str) or not CANVAS_RE.fullmatch(cid) or not any(c['id'] == cid for c in project['canvases']):
            raise CanvasError('没有这张画布。', 404)
        return self._project_dir(pid) / 'canvases' / f'{cid}.json'

    def add_canvas(self, pid, name=None):
        with self.lock:
            project = self.project(pid)
            if len(project['canvases']) >= MAX_CANVASES:
                raise CanvasError(f'一个项目最多 {MAX_CANVASES} 张画布。')
            numbers = [int(m.group(1)) for c in project['canvases'] if (m := re.fullmatch(r'画布 (\d+)', c['name']))]
            canvas = {'id': uuid.uuid4().hex[:8], 'name': clean_name(name, f'画布 {max(numbers, default=0) + 1}')}
            project['canvases'].append(canvas)
            self._save_project(project)
            return {'project': project, 'canvas': canvas}

    def rename_canvas(self, pid, cid, name):
        with self.lock:
            project = self.project(pid)
            self._canvas_path(pid, cid, project)
            for canvas in project['canvases']:
                if canvas['id'] == cid:
                    canvas['name'] = clean_name(name, canvas['name'])
            return self._save_project(project)

    def delete_canvas(self, pid, cid):
        with self.lock:
            project = self.project(pid)
            path = self._canvas_path(pid, cid, project)
            if len(project['canvases']) <= 1:
                raise CanvasError('项目里至少要留一张画布。')
            project['canvases'] = [c for c in project['canvases'] if c['id'] != cid]
            if path.is_file():
                self._ensure_dirs()
                os.replace(path, self.trash / f'{pid}-{cid}-{int(time.time())}.json')
            return self._save_project(project)

    def board(self, pid, cid):
        path = self._canvas_path(pid, cid)
        try:
            data = _read(path)
        except (FileNotFoundError, ValueError):
            data = None
        return data if isinstance(data, dict) else dict(EMPTY_BOARD)

    def save_board(self, pid, cid, data):
        if not isinstance(data, dict) or not isinstance(data.get('nodes'), list) or not isinstance(data.get('edges'), list):
            raise CanvasError('画布格式无效。')
        if len(data['nodes']) > MAX_NODES or len(data['edges']) > MAX_EDGES:
            raise CanvasError('画布上的节点太多了。')
        if len(json.dumps(data, ensure_ascii=False)) > MAX_BOARD_BYTES:
            raise CanvasError('画布内容太大。', 413)
        with self.lock:
            project = self.project(pid)
            _write(self._canvas_path(pid, cid, project), {**data, 'version': 2})
            self._save_project(project)
        return {'saved': True}

    # ── 记住的设置（生图参数、自己的提示词预设、上次打开的画布） ──
    def prefs(self):
        try:
            data = _read(self.root / 'prefs.json')
        except (FileNotFoundError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        gen = data.get('gen') if isinstance(data.get('gen'), dict) else {}
        return {'gen': {**DEFAULT_GEN, **self._clean_gen(gen)},
                'presets': [p for p in data.get('presets') or [] if isinstance(p, str)][:MAX_PRESETS],
                'last': data.get('last') if isinstance(data.get('last'), dict) else None}

    @staticmethod
    def _clean_gen(gen):
        cleaned = {}
        if gen.get('ratio') in RATIOS:
            cleaned['ratio'] = gen['ratio']
        if gen.get('quality') in QUALITIES:
            cleaned['quality'] = gen['quality']
        if gen.get('resolution') in RESOLUTIONS:
            cleaned['resolution'] = gen['resolution']
        if isinstance(gen.get('count'), int) and 1 <= gen['count'] <= MAX_COUNT:
            cleaned['count'] = gen['count']
        return cleaned

    def save_prefs(self, data):
        if not isinstance(data, dict):
            raise CanvasError('设置格式无效。')
        with self.lock:
            prefs = self.prefs()
            if isinstance(data.get('gen'), dict):
                prefs['gen'].update(self._clean_gen(data['gen']))
            if 'presets' in data:
                presets = data['presets']
                if not isinstance(presets, list) or len(presets) > MAX_PRESETS:
                    raise CanvasError(f'提示词预设最多 {MAX_PRESETS} 条。')
                prefs['presets'] = list(dict.fromkeys(str(p).strip()[:500] for p in presets if str(p).strip()))
            if isinstance(data.get('last'), dict):
                last = data['last']
                if PROJECT_RE.fullmatch(str(last.get('project', ''))) and CANVAS_RE.fullmatch(str(last.get('canvas', ''))):
                    prefs['last'] = {'project': last['project'], 'canvas': last['canvas']}
            _write(self.root / 'prefs.json', prefs)
            return prefs

    # ── 图片文件 ──
    def images_dir(self, pid):
        folder = self._project_dir(pid) / 'images'
        if not folder.is_dir():
            raise CanvasError('没有这个项目（可能已删除）。', 404)
        return folder

    def file_path(self, pid, name):
        if not isinstance(name, str) or not FILE_RE.fullmatch(name):
            raise CanvasError('图片文件名无效。', 404)
        path = self.images_dir(pid) / name
        if not path.is_file():
            raise CanvasError('图片不存在。', 404)
        return path

    def thumb_path(self, pid, name, width):
        """画布里的小图用缩略图：按需用 Pillow 生成，缓存在项目的 thumbs 文件夹（WebP，几十 KB）。
        节点用 1280、参考图栏 / @ 列表 / 项目卡片用 256；看大图、复制图片、拿去生图仍用原图。
        原图本身就不大、或者缩图失败时直接给原图。"""
        source = self.file_path(pid, name)
        try:
            width = int(width)
        except (TypeError, ValueError):
            return source
        size = THUMB_SIZES[-1] if width > THUMB_SIZES[0] else THUMB_SIZES[0]
        target = self._project_dir(pid) / 'thumbs' / f'{Path(name).stem}-{size}.webp'
        try:
            if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
                return target
            with Image.open(source) as image:
                if max(image.size) <= size * 1.25:
                    return source
                image.seek(0)
                image = ImageOps.exif_transpose(image)
                if image.mode not in ('RGB', 'RGBA'):
                    image = image.convert('RGBA' if 'A' in image.getbands() or 'transparency' in image.info else 'RGB')
                image.thumbnail((size, size), Image.LANCZOS)
                target.parent.mkdir(exist_ok=True)
                temp = target.with_name(f'{target.stem}.{uuid.uuid4().hex[:8]}.tmp')
                image.save(temp, 'WEBP', quality=86, method=4)
            os.replace(temp, target)
            return target
        except Exception:
            return source

    def _store(self, pid, content, extension=None):
        try:
            with Image.open(io.BytesIO(content)) as image:
                kind = UPLOAD_FORMATS.get(image.format or '')
                width, height = ImageOps.exif_transpose(image).size   # 按照片方向信息转正后的尺寸
        except Exception:
            raise CanvasError('只接受 jpg、png、webp、gif、bmp 图片。') from None
        if not kind:
            raise CanvasError('只接受 jpg、png、webp、gif、bmp 图片。')
        folder = self.images_dir(pid)
        name = f'{uuid.uuid4().hex}.{extension or kind}'
        temp = folder / (name + '.part')
        temp.write_bytes(content)
        os.replace(temp, folder / name)
        return {'file': name, 'w': width, 'h': height}

    def add_bytes(self, pid, content):
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise CanvasError('单张图片不能超过 50 MB。', 413)
        return self._store(pid, content)

    def add_paths(self, pid, paths):
        if not isinstance(paths, list) or not 1 <= len(paths) <= 50:
            raise CanvasError('一次最多放 50 张图片。')
        added = []
        for value in paths:
            path = Path(str(value))
            if not path.is_absolute() or not path.is_file() or path.stat().st_size > MAX_UPLOAD_BYTES:
                continue
            try:
                added.append(self.add_bytes(pid, path.read_bytes()))
            except CanvasError:
                continue
        if not added:
            raise CanvasError('没有能用的图片（只接受 jpg、png、webp、gif、bmp）。')
        return added

    def copy_files(self, pid, source, names):
        """粘贴节点到别的项目时，把图片从原项目拷过来。文件名本来就是随机的，拷过来不改名，节点里的记录不用动。"""
        if not isinstance(names, list) or not 1 <= len(names) <= 200:
            raise CanvasError('一次最多拷 200 张图片。')
        folder = self.images_dir(pid)
        copied = []
        for name in dict.fromkeys(names):
            try:
                path = self.file_path(source, name)
            except CanvasError:
                continue                       # 原图已经没了：这个节点粘过来会显示空白，不影响别的
            target = folder / name
            if not target.exists():
                shutil.copy2(path, target)
            copied.append(name)
        return {'files': copied}

    def reveal(self, pid=None, name=None):
        """在资源管理器里打开：某张图（选中它）、某个项目的图片文件夹，或所有项目的总文件夹。"""
        if pid is None:
            target, select = self.projects, False
        elif name:
            target, select = self.file_path(pid, name), True
        else:
            target, select = self.images_dir(pid), False
        if os.name == 'nt':
            command = ['explorer', '/select,', str(target)] if select else ['explorer', str(target)]
            subprocess.Popen(command)   # noqa: S603 只打开画布自己的文件夹
        return {'opened': True}

    # ── 旧版迁移 ──
    def _migrate(self):
        old_board, old_files = self.root / 'board.json', self.root / 'files'
        if not old_board.is_file() and not old_files.is_dir():
            return
        with self.lock:
            project = self.create_project('我的项目')
            pid, cid = project['id'], project['canvases'][0]['id']
            if old_board.is_file():
                os.replace(old_board, self._project_dir(pid) / 'canvases' / f'{cid}.json')
            if old_files.is_dir():
                for path in old_files.iterdir():
                    if FILE_RE.fullmatch(path.name):
                        os.replace(path, self._project_dir(pid) / 'images' / path.name)
                shutil.rmtree(old_files, ignore_errors=True)
            prefs = self.prefs()
            prefs['last'] = {'project': pid, 'canvas': cid}
            _write(self.root / 'prefs.json', prefs)

    # ── 生图 ──
    def _run_path(self, run_id):
        if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id):
            raise CanvasError('生成任务号无效。', 404)
        return self.runs / (run_id + '.json')

    def run(self, run_id):
        path = self._run_path(run_id)
        if not path.is_file():
            raise CanvasError('没有这个生成任务。', 404)
        return _read(path)

    def _write_run(self, record):
        _write(self._run_path(record['id']), record)

    def _recover(self):
        """程序启动时整理生成记录：
        - 上次关掉时还在生成的：标成失败，网页上可以重试；
        - 30 天前的记录删掉（每生成一次就有一个记录文件，不清理会一直变多）。图已经在项目的图片文件夹里，不受影响。
        启动时没有正在生成的任务，所以不会删到还在用的记录。"""
        cutoff = time.time() - RUN_KEEP_DAYS * 86400
        for path in self.runs.glob('*.json'):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    continue
                record = _read(path)
                if record.get('state') == 'running':
                    record.update(state='failed', error='程序关闭，生成被中断，请重试。')
                    self._write_run(record)
            except (OSError, ValueError, AttributeError):
                continue

    def generate(self, data):
        if not isinstance(data, dict):
            raise CanvasError('请求格式无效。')
        pid = str(data.get('project') or '')
        self.images_dir(pid)
        prompt = str(data.get('prompt') or '').strip()
        if not prompt:
            raise CanvasError('先写提示词。')
        ratio = data.get('ratio') if data.get('ratio') in RATIOS else '1:1'
        quality = data.get('quality') if data.get('quality') in QUALITIES else 'standard'
        resolution = data.get('resolution') if data.get('resolution') in RESOLUTIONS else '2K'
        try:
            count = max(1, min(MAX_COUNT, int(data.get('count') or 1)))
        except (TypeError, ValueError):
            count = 1
        refs = data.get('refs') or []
        if not isinstance(refs, list) or len(refs) > MAX_REFS:
            raise CanvasError(f'参考图最多 {MAX_REFS} 张。')
        ref_paths = [self.file_path(pid, name) for name in refs]
        channel = str(data.get('channel') or '')
        if channel and not CHANNEL_RE.fullmatch(channel):
            raise CanvasError('生图渠道号无效。')
        record = {'id': uuid.uuid4().hex, 'state': 'running', 'project': pid, 'node': str(data.get('node') or '')[:40],
                  'prompt': prompt, 'ratio': ratio, 'quality': quality, 'resolution': resolution, 'count': count,
                  'refs': refs, 'channel': channel, 'files': [], 'error': '', 'started': time.time(), 'finished': None}
        self._write_run(record)
        threading.Thread(target=self._work, args=(record, ref_paths), daemon=True, name='画布生图').start()
        return record

    def _work(self, record, ref_paths):
        try:
            with self.slots:
                results = self.generator.generate(record['prompt'], ref_paths, record['ratio'], record['count'],
                                                  record['quality'], record['resolution'], record.get('channel') or None)
            # 出图后对比例：差一点的居中裁准；方向反了的不裁，提示一下
            fitted = [fit_ratio(content, extension, record['ratio']) for content, extension in results]
            files = [self._store(record['project'], content, extension) for content, extension, _ in fitted]
            notes = [note for *_, note in fitted if note]
            missing = '' if len(files) == record['count'] else f'只生成了 {len(files)} 张（其余失败）。'
            record.update(state='done', files=files, error=missing + (notes[0] + '。' if notes else ''))
        except (AIError, CanvasError, OSError) as error:
            record.update(state='failed', error=str(error) or '生成失败。')
        except Exception as error:   # 后台线程：任何意外都要落到任务状态里，不能一直显示「生成中」
            record.update(state='failed', error='生成失败：' + str(error))
        record['finished'] = time.time()
        try:
            self._write_run(record)
        except OSError:
            pass
