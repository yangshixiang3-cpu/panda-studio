"""本地任务服务；颜色与缩图使用已有 Pillow，文件协议以 docs/SPEC.md 为准。"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .paths import IMAGE_EXTENSIONS
from .lighting import (MAX_LIGHTING_BYTES, validate_lighting, merged_lighting, lighting_text,
                       effective_lighting, lighting_active, protect_light_overrides, render_lighting_config)
from .jobs import JobIndex, TERMINAL
from .runner import RunnerStatus

try:
    from . import materials as _materials
except ImportError:
    _materials = None


def _resolve_materials():
    global _materials
    if _materials is not None:
        return _materials
    import sys
    mod_key = f'{__package__}.materials' if __package__ else 'server.materials'
    if mod_key in sys.modules:
        return sys.modules[mod_key]
    try:
        import importlib
        mod = importlib.import_module(mod_key)
        _materials = mod
        return mod
    except ImportError:
        return None


def _fallback_effective_overrides(task):
    accepted = task.get('ai_calibration', {}).get('accepted_overrides')
    manual = task.get('material_overrides')
    if not isinstance(manual, dict) or not manual:
        return accepted
    base = copy.deepcopy(accepted) if isinstance(accepted, dict) else {
        'materials': {}, 'lights': {}, 'environment': {}, 'items': {}
    }
    for section in ('materials', 'lights', 'environment', 'items'):
        base.setdefault(section, {})
    if isinstance(manual.get('materials'), dict):
        for name, params in manual['materials'].items():
            if isinstance(params, dict):
                merged = dict(base['materials'].get(name) or {})
                merged.update(params)
                base['materials'][name] = merged
    if isinstance(manual.get('environment'), dict):
        base['environment'].update(manual['environment'])
    return base


def _fallback_scene_materials_detail(studio, task, jobs=None):
    materials = []
    environment = {}
    task_folder = studio.task_dir(task['id'])
    candidates = []
    by_id = {job['job_id']: job for job, _ in (jobs or [])}
    for record in reversed(task.get('ai_calibration', {}).get('rounds', [])):
        cal_dir = by_id.get(record.get('job_id'), {}).get('calibration_dir')
        folder = Path(cal_dir) if cal_dir else task_folder / 'calibration' / f"round-{record['n']}"
        candidates.append(folder / 'scene_manifest.json')
    candidates.append(task_folder / 'preview' / 'scene_manifest.json')
    candidates.append(task_folder / 'preview' / 'materials.json')
    for path in candidates:
        if not inside(path, task_folder) or not path.is_file():
            continue
        try:
            doc = read_json(path)
            raw_mats = doc.get('materials')
            if isinstance(raw_mats, list):
                seen = set()
                for entry in raw_mats:
                    if isinstance(entry, dict) and isinstance(entry.get('name'), str) and entry['name'] and entry['name'] not in seen:
                        seen.add(entry['name'])
                        materials.append(entry)
                if isinstance(doc.get('environment'), dict):
                    environment = doc['environment']
                break
        except (OSError, ValueError, TypeError):
            continue
    return {
        'materials': materials,
        'environment': environment,
        'material_overrides': copy.deepcopy(task.get('material_overrides') or {}),
        'effective_overrides': _fallback_effective_overrides(task),
        'material_bindings': copy.deepcopy(task.get('material_bindings') or {}),
    }


def _fallback_update_task_materials(studio, task_id, data):
    if not isinstance(data, dict):
        raise StudioError('材质参数格式无效。')
    with studio.lock:
        from .calibration import ACTIVE
        task = studio.load(task_id)
        if task.get('ai_calibration', {}).get('state') in ACTIVE:
            raise StudioError(CALIBRATION_EDIT_CONFLICT, 409)
        if data.get('reset') is True:
            task.pop('material_overrides', None)
        else:
            current = copy.deepcopy(task.get('material_overrides') or {})
            incoming = data.get('material_overrides') if isinstance(data.get('material_overrides'), dict) else data
            if 'materials' in incoming:
                if not isinstance(incoming['materials'], dict):
                    raise StudioError('材质覆盖参数必须是对象。')
                mats = dict(current.get('materials') or {})
                for name, values in incoming['materials'].items():
                    if values is None:
                        mats.pop(name, None)
                    elif isinstance(values, dict):
                        mats[name] = {k: v for k, v in values.items() if type(v) in (int, float) and math.isfinite(v) or v is None}
                    else:
                        raise StudioError('材质参数项无效。')
                current['materials'] = mats
            if 'environment' in incoming:
                if incoming['environment'] is None:
                    current.pop('environment', None)
                elif isinstance(incoming['environment'], dict):
                    current['environment'] = {
                        k: v for k, v in incoming['environment'].items()
                        if type(v) in (int, float) and math.isfinite(v)
                    }
                else:
                    raise StudioError('环境反射参数必须是对象。')
            if current.get('materials') or current.get('environment'):
                task['material_overrides'] = current
            else:
                task.pop('material_overrides', None)
        if 'material_bindings' in data:
            if not isinstance(data['material_bindings'], dict):
                raise StudioError('材质绑定必须是对象。')
            task['material_bindings'] = {
                str(k): v for k, v in data['material_bindings'].items()
                if isinstance(k, str) and (v is None or isinstance(v, (str, dict)))
            }
        studio.save(task)
        jobs = studio.job_index.for_task(task_id)
        return {'task': task, 'materials_detail': _fallback_scene_materials_detail(studio, task, jobs)}

ROOT = Path(__file__).resolve().parents[1]
IMAGES = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}
MAX_REFERENCE_BYTES = 50 * 1024 * 1024
SKIP_MODEL_DIRS = {'maps', 'sku', '视角图', '旧版', 'autoback', '参考图', '_extra',
                   'smallshots', 'final', 'preview', 'exchange', 'node_modules', '.git'}
ID_RE = re.compile(r'^\d{8}-\d{6}-[0-9a-f]{4}$')
VARIANT_ID_RE = re.compile(r'[A-Za-z0-9_]{1,40}')
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\ud800-\udfff]')
INVALID_UNICODE = re.compile(r'[\ud800-\udfff]')
WINDOWS_DEVICE_RE = re.compile(r'(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³])', re.I)
# 白底模板随程序内置（用户提供的那一份），不再让用户选择。
TEMPLATE_FILE = Path('template')/'白底模板.max'
# 只导入产品的 .max；FBX 不再作为模型选项（预览用的 FBX 由 Max 从 .max 导出）。
MODEL_SUFFIXES = {'.max'}
# 「保存到」文件夹列表只看第一层，避免选了大目录时扫描太久。
DELIVERY_LIST_LIMIT = 200
CALIBRATION_EDIT_CONFLICT = 'AI 校准进行中：结束后再改打光或清单。'
CALIBRATION_RENDER_CONFLICT = 'AI 校准进行中，结束后再出图。'
# 正式图输出档位（竖版 3:4，横版宽高对调）。用户定：4K=3000×4000，6K=4500×6000，8K=6000×8000。
DEFAULT_OUTPUT_PRESET = '4K'
OUTPUT_PRESETS = {
    '4K': {'portrait': [3000, 4000], 'landscape': [4000, 3000]},
    '6K': {'portrait': [4500, 6000], 'landscape': [6000, 4500]},
    '8K': {'portrait': [6000, 8000], 'landscape': [8000, 6000]},
}


def output_presets(config):
    """config.json 可用 output_presets 覆盖；缺省用内置三档。"""
    presets = config.get('output_presets')
    return presets if isinstance(presets, dict) and presets else OUTPUT_PRESETS


class StudioError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now().astimezone().isoformat(timespec='microseconds')


def new_id():
    return datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:4]


def inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def backup(path):
    """为需要保留上一版的非原子输出保留一个副本。"""
    path = Path(path)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + '.prev'))


def atomic_json(path, data, immutable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        raise StudioError('任务文件已经存在，不能改写。', 409)
    temp = path.parent / (uuid.uuid4().hex[:8] + '.tmp')
    with temp.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    # Windows 上并行读取状态文件时，文件句柄可能短暂阻止替换。
    for attempt in range(30):
        try:
            os.replace(temp, path)
            break
        except PermissionError:
            if attempt == 29:raise
            time.sleep(.02)


def read_json(path):
    for attempt in range(30):
        try:
            with Path(path).open(encoding='utf-8-sig') as stream:
                return json.load(stream)
        except PermissionError:
            if attempt == 29:raise
            time.sleep(.02)


def validate_reference(name, content, size=None):
    suffix = Path(name).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        raise StudioError('只接受 jpg、jpeg、png、webp 图片。',415)
    size = len(content) if size is None else size
    if not 0 < size <= MAX_REFERENCE_BYTES:
        raise StudioError('单张参考图必须大于 0 且不超过 50 MB。',413)
    valid = ((suffix in {'.jpg', '.jpeg'} and content.startswith(b'\xff\xd8\xff')) or
             (suffix == '.png' and content.startswith(b'\x89PNG\r\n\x1a\n')) or
             (suffix == '.webp' and content[:4] == b'RIFF' and content[8:12] == b'WEBP'))
    if not valid:
        raise StudioError('图片内容与扩展名不符，请选择有效图片。',415)


def basis(azimuth, elevation):
    a, e = map(math.radians, (azimuth, elevation))
    return ([math.sin(a)*math.cos(e), math.sin(e), math.cos(a)*math.cos(e)],
            [math.cos(a), 0, -math.sin(a)],
            [-math.sin(a)*math.sin(e), math.cos(e), -math.cos(a)*math.sin(e)])


def corners(size):
    return [[size[i] * (0.5 if mask >> i & 1 else -0.5) for i in range(3)] for mask in range(8)]


def dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def fit_camera(size, azimuth, elevation, aspect, fov=27):
    back, right, up = basis(azimuth, elevation)
    ty = math.tan(math.radians(fov)/2)*0.8
    tx = ty*aspect
    distance = max(max(dot(p, back)+abs(dot(p, right))/tx,
                       dot(p, back)+abs(dot(p, up))/ty) for p in corners(size))
    distance = max(distance, 0.001)
    return {'frame': 'PF1', 'position_mm': [v*distance for v in back],
            'target_mm': [0.0, 0.0, 0.0], 'fov_y_deg': fov, 'vertical_correction': False,
            'azimuth_deg': azimuth, 'elevation_deg': elevation, 'distance_mm': distance}


def orientation_for(size, azimuth, elevation):
    """以初始正交投影决定画幅，再按该画幅求透视取景。"""
    _, right, up = basis(azimuth, elevation)
    x = [dot(p, right) for p in corners(size)]
    y = [dot(p, up) for p in corners(size)]
    return 'landscape' if (max(x)-min(x))/max(max(y)-min(y), 1e-9) > 1.3 else 'portrait'


def draft_items(size=(1200, 850, 400), parts=()):
    a, e = 35, 12
    orient = orientation_for(size, a, e)
    return [{'no':1, 'label':'主图', 'view':'main', 'orientation':orient, 'enabled':True,
             'camera':fit_camera(size,a,e,4/3 if orient=='landscape' else 3/4),
             'lighting':None, 'lighting_text':'模板默认灯光',
             'parts':{p['name']:0.0 for p in parts}, 'render_open_state':False,
             'smallshot':{'state':'none','file':'smallshots/1.jpg','job_id':''},
             'final':{'state':'none','file':'final/1.jpg','mask':'final/1_遮罩_ID.jpg','job_id':''},
             'note':''}]


def validate_variant_id(value):
    if not isinstance(value, str) or not VARIANT_ID_RE.fullmatch(value):
        raise StudioError('款式编号必须为 1–40 位英文字母、数字或下划线。')
    return value


def validate_variant_name(value):
    if not isinstance(value, str):
        raise StudioError('款式名必须为 1–40 字。')
    name = INVALID_FILENAME_CHARS.sub('', value).strip().rstrip('. ')
    if not 1 <= len(name) <= 40 or ('.' in name and WINDOWS_DEVICE_RE.fullmatch(name.split('.')[0].rstrip())):
        raise StudioError('请使用 1–40 字且可用于 Windows 文件名的款式名。')
    return name


def validate_variant_names(variants):
    names = [variant['name'].casefold() for variant in variants]
    if len(set(names)) != len(names):
        raise StudioError('款式名重复，请使用不同的名称。')


def validate_variants(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise StudioError('请提供 1–20 个柜子。')
    variants, ids, seen_nodes = [], set(), set()
    for entry in value:
        if not isinstance(entry, dict):
            raise StudioError('款式信息无效。')
        variant_id = validate_variant_id(entry.get('id'))
        if variant_id in ids:
            raise StudioError('款式编号不能重复。')
        ids.add(variant_id)
        name = validate_variant_name(entry.get('name'))
        nodes = entry.get('nodes')
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= 2000 or not all(
                isinstance(node, str) and 1 <= len(node) <= 255 and not INVALID_UNICODE.search(node)
                for node in nodes):
            raise StudioError('每个柜子需要 1–2000 个节点，每个节点名为 1–255 字。')
        if seen_nodes.intersection(nodes):
            raise StudioError('不同柜子的节点名不能重叠。')
        seen_nodes.update(nodes)
        bbox = entry.get('bbox_mm')
        if not isinstance(bbox, dict):
            raise StudioError('柜子包围盒无效。')
        bounds = {}
        for key in ('min', 'max'):
            point = bbox.get(key)
            try:
                valid = isinstance(point, list) and len(point) == 3 and all(
                    type(coord) in (int, float) and math.isfinite(coord) for coord in point)
            except OverflowError:
                valid = False
            if not valid:
                raise StudioError('柜子包围盒的最小值和最大值各需要三个有限毫米数值。')
            bounds[key] = point[:]
        if any(low > high for low, high in zip(bounds['min'], bounds['max'])):
            raise StudioError('柜子包围盒的最小值不能大于最大值。')
        variants.append({'id':variant_id, 'name':name, 'nodes':nodes[:], 'bbox_mm':bounds})
    validate_variant_names(variants)
    return variants


# 按清单编号命名的成果文件（文件夹, 编号后缀）。删除、排序时随条目一起改名。
RESULT_FILES = (('smallshots', '.jpg'), ('final', '.jpg'), ('final', '_遮罩_ID.jpg'),
                ('lighting', '.png'), ('lighting', '.png.prev'))
REMOVED_KEEP = 20       # 删掉的条目留最近 20 个，撤销时连同成果一起恢复
THUMB_MIN = 32
THUMB_MAX = 1024
THUMB_KEEP_DAYS = 30


def replace_retry(source, target):
    # Windows 上图片正被读取（预览、缩图）时改名会短暂失败。
    for attempt in range(30):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 29:raise
            time.sleep(.02)


def undo_moves(staged, placed):
    for source, temp, target in reversed(placed):
        try:os.replace(target, temp)
        except OSError:pass
    for source, temp, target in reversed(staged):
        try:os.replace(temp, source)
        except OSError:pass


def move_files(moves):
    """成组改名：先全部移到临时名再放到目标，编号互换也不会互相覆盖；中途失败按原路退回。"""
    staged, placed = [], []
    try:
        for source, target in moves:
            if not source.is_file():continue
            temp = source.with_name(f'.{uuid.uuid4().hex[:8]}.moving')
            replace_retry(source, temp)
            staged.append((source, temp, target))
        for source, temp, target in staged:
            target.parent.mkdir(parents=True, exist_ok=True)
            replace_retry(temp, target)
            placed.append((source, temp, target))
    except OSError:
        undo_moves(staged, placed)
        raise StudioError('整理出图文件失败：请关掉正在打开这些图片的程序后再试。', 409)
    return staged, placed


def renumbered_result(result, old, new, folder):
    """成果跟着条目走：按编号命名的文件改成新编号，状态和作业号不变；
    记下作业里的原编号（job_item），合并作业状态时按它对应。"""
    result = copy.deepcopy(result)
    if old == new:
        return result
    for key, suffix in (('file', '.jpg'), ('mask', '_遮罩_ID.jpg')):
        if str(result.get(key) or '').replace('\\', '/') == f'{folder}/{old}{suffix}':
            result[key] = f'{folder}/{new}{suffix}'
    if result.get('job_id') and (result.get('job_item') or {}).get('job_id') != result['job_id']:
        result['job_item'] = {'job_id':result['job_id'], 'no':old}
    return result


def delivery_stem(task, no):
    variant = task.get('variant')
    return f"{variant['name']}_{no}" if variant else str(no)


def validate_camera(value):
    if not isinstance(value, dict) or value.get('frame') != 'PF1':
        raise StudioError('画面坐标必须为 PF1。')
    result = {'frame':'PF1'}
    for key in ('position_mm','target_mm'):
        arr = value.get(key)
        if not isinstance(arr,list) or len(arr)!=3 or not all(isinstance(v,(float,int)) and math.isfinite(v) and abs(v)<1e10 for v in arr):
            raise StudioError('画面位置需要三个有效毫米数值。')
        result[key] = arr[:]
    fov = value.get('fov_y_deg',27)
    if not isinstance(fov,(float,int)) or not math.isfinite(fov) or not 1 <= fov <= 120:
        raise StudioError('透视数值无效。')
    correction = value.get('vertical_correction', False)
    if not isinstance(correction, bool):
        raise StudioError('镜头校正必须为布尔值。')
    delta = [x-y for x,y in zip(result['position_mm'],result['target_mm'])]
    distance = math.hypot(*delta)
    if distance < 0.001:
        raise StudioError('画面观察位置与中心不能重合。')
    result.update(fov_y_deg=fov, vertical_correction=correction, azimuth_deg=math.degrees(math.atan2(delta[0],delta[2])),
                  elevation_deg=math.degrees(math.atan2(delta[1],math.hypot(delta[0],delta[2]))), distance_mm=distance)
    return result


class Studio:
    def __init__(self, root=ROOT, config=None, defer_startup=False):
        self.root = Path(root).resolve()
        self.config = config or read_json(self.root/'config.json')
        self.exchange = (self.root/self.config['exchange_dir']).resolve()
        if self.exchange == self.root or not inside(self.exchange,self.root) or any(inside(self.exchange,self.root/p) for p in ('max','docs','web','server','tools')):
            raise StudioError('交换目录必须位于白底工具的可写目录内。')
        self.lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._delivering = set()
        self.scanned_models = {}
        self._defer_startup = bool(defer_startup)
        self._startup_hooks = []
        self._startup_once_lock = threading.Lock()
        self._startup_ran = False
        self.startup_done = threading.Event()
        (self.exchange/'tasks').mkdir(parents=True,exist_ok=True)
        (self.exchange/'jobs').mkdir(parents=True,exist_ok=True)
        self.job_index = JobIndex(self.exchange/'jobs', read_json, defer_initial_scan=self._defer_startup)
        self.runner = RunnerStatus(self.root, self.exchange, self.config)
        from .remote_client import RemoteBridge
        self.remote = RemoteBridge(self)   # 内网渲染机：选了渲染机时转发作业（后台线程由 LocalServer 启动）
        from .calibration import Calibration
        from .ref_library import RefLibrary
        self.refs = RefLibrary(self.root)
        self.calibration = Calibration(self)
        if not self._defer_startup:
            self.calibration.recover()
            # 以前 AI 校准里加的材质参考补进参考库（只补没收进去的，后台跑，不耽误打开）
            threading.Thread(target=self.calibration.sync_library, daemon=True, name='refs-sync').start()
            self.purge_trash()
            self.purge_thumbs()
            self._startup_ran = True
            self.startup_done.set()

    def register_startup_hook(self, fn):
        self._startup_hooks.append(fn)

    def run_startup_tasks(self):
        with self._startup_once_lock:
            if self._startup_ran:
                return
            self._startup_ran = True
        try:
            from . import node_setup
            for step in (
                self.job_index.refresh_history,
                self.calibration.recover,
                self.purge_trash,
                self.purge_thumbs,
                self.calibration.sync_library,
                self.runner.warm_async,
                lambda: node_setup.warm_async(self.root),
                *self._startup_hooks,
            ):
                if self.calibration.stopping.is_set():
                    break
                try:
                    step()
                except Exception:
                    continue
        finally:
            self.startup_done.set()

    def task_dir(self, task_id):
        if not ID_RE.fullmatch(task_id):
            raise StudioError('任务号无效。',404)
        result = self.exchange/'tasks'/task_id
        if not inside(result,self.exchange/'tasks'):
            raise StudioError('任务路径越界。',403)
        return result

    def load(self, task_id):
        path = self.task_dir(task_id)/'task.json'
        if not path.is_file():
            raise StudioError('没有找到这个任务。',404)
        task = read_json(path)
        from .calibration import initial_calibration
        task.setdefault('ai_calibration', initial_calibration(self.config))
        task['lighting'] = validate_lighting(task.get('lighting'))
        for item in task['items']:
            item.setdefault('item_id', f"{task_id}:{item['no']}")
            item['camera'].setdefault('vertical_correction', False)
            item['lighting'] = validate_lighting(item['lighting']) if item.get('lighting') is not None else None
            item['lighting_text'] = lighting_text(merged_lighting(task, item))
        return task

    def save(self, task):
        task['lighting'] = validate_lighting(task.get('lighting'))
        for item in task['items']:
            item.setdefault('item_id', uuid.uuid4().hex)
            item['lighting'] = validate_lighting(item['lighting']) if item.get('lighting') is not None else None
            item['lighting_text'] = lighting_text(merged_lighting(task, item))
        path = self.task_dir(task['id'])/'task.json'
        if not path.is_file() or read_json(path) != task:
            atomic_json(path,task)

    def product(self, path):
        if not path or not Path(path).is_absolute() or not Path(path).is_dir():
            raise StudioError('请输入存在的产品文件夹完整路径。')
        return Path(path).resolve()

    def template_path(self):
        return (self.root/TEMPLATE_FILE).resolve()

    def write_job(self, job):
        """写作业文件。渲染位置选的是内网渲染机时，先登记给转发线程（写带 remote 标记的状态），本机执行器不会接。"""
        self.remote.claim(job)
        atomic_json(self.exchange/'jobs'/(job['job_id']+'.json'),job,immutable=True)

    def runner_status(self):
        """界面上的执行器状态：本机 Max，或当前选的内网渲染机。"""
        if self.remote.target() is not None:
            return self.remote.runner_status()
        return {**self.runner.status(), 'target': None}

    def _product_prefs(self):
        try:
            data = read_json(self.exchange/'products.json')
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def delivery_dir(self, product):
        """成品保存位置：用户为这个产品指定过文件夹就用它，否则是产品下的 sku。返回 (路径, 是否自定义)。"""
        product = Path(product).resolve()
        custom = (self._product_prefs().get(str(product).casefold()) or {}).get('delivery_dir')
        if isinstance(custom, str) and custom:
            return Path(custom), True
        return self.product_root(product, 'sku'), False

    def set_delivery_dir(self, product_dir, folder):
        product = self.product(product_dir)
        text = str(folder or '').strip().strip('"')
        with self.lock:
            prefs = self._product_prefs()
            key = str(product).casefold()
            entry = prefs.get(key) if isinstance(prefs.get(key), dict) else {}
            if text:
                target = Path(text)
                if not target.is_absolute() or not target.is_dir():
                    raise StudioError('找不到这个文件夹。')
                target = target.resolve()
                own = [self.exchange] + [self.root/p for p in ('web', 'server', 'max', 'template', 'refs', 'tools', 'pylibs', 'secrets', 'webview-data')]
                if target == self.root or any(inside(target, p) for p in own):
                    raise StudioError('不能保存到工作台自己的文件夹里。', 403)
                if target == (product/'sku').resolve():
                    entry.pop('delivery_dir', None)
                else:
                    entry['delivery_dir'] = str(target)
            else:
                entry.pop('delivery_dir', None)
            if entry:
                prefs[key] = entry
            else:
                prefs.pop(key, None)
            atomic_json(self.exchange/'products.json', prefs)
        return self.scan(str(product))

    def product_root(self, product, name):
        root = product/name
        if root.is_symlink() or getattr(root,'is_junction',lambda:False)() or not inside(root,product):
            raise StudioError('产品子目录指向了外部路径。',403)
        return root

    def scan(self, path):
        product = self.product(path)
        # 保留原有的根目录链接校验；递归中也不跟随目录链接或联接。
        for name in ('模型', '参考图', 'sku'):
            self.product_root(product, name)
        models, references = [], []
        def walk(folder, depth=0):
            for child in sorted(folder.iterdir(), key=lambda p: str(p).casefold()):
                if child.is_symlink() or getattr(child, 'is_junction', lambda: False)() or not inside(child, product):
                    continue
                if child.is_file() and child.suffix.lower() in MODEL_SUFFIXES:
                    models.append(str(child.resolve()))
                elif child.is_dir():
                    if depth < 5 and not child.name.startswith('.') and child.name.casefold() not in SKIP_MODEL_DIRS:
                        walk(child, depth + 1)
        walk(product)
        # 参考图单独遍历：不受模型的五层与目录排除规则限制。
        for current, directories, names in os.walk(product, followlinks=False):
            folder = Path(current)
            directories[:] = [name for name in directories if not (folder/name).is_symlink()
                              and not getattr(folder/name, 'is_junction', lambda: False)()
                              and inside(folder/name, product)]
            if '参考图' in folder.relative_to(product).parts:
                references.extend(str((folder/name).resolve()) for name in names
                                  if (folder/name).suffix.lower() in IMAGES
                                  and not (folder/name).is_symlink() and inside(folder/name, product))
        models.sort(key=lambda p: ('模型' not in Path(p).relative_to(product).parts[:-1], p.casefold()))
        with self._scan_lock:
            self.scanned_models[str(product)] = set(models)
        delivery, custom = self.delivery_dir(product)
        try:
            saved = sorted((str(p) for p in delivery.iterdir() if p.is_file()), key=str.casefold)[:DELIVERY_LIST_LIMIT] if delivery.is_dir() else []
        except OSError:
            saved = []
        return {'product_dir':str(product),'product_name':product.name,
                'models':models,'reference_images':sorted(set(references), key=str.casefold),
                'sku_exists':delivery.is_dir(),'sku_files':saved,
                'delivery_dir':str(delivery),'delivery_custom':custom}

    def browse(self, path=''):
        if not path:
            drives = [f'{c}:\\' for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if Path(f'{c}:\\').is_dir()] if os.name=='nt' else ['/']
            return {'path':'','parent':None,'directories':[{'name':p,'path':p} for p in drives]}
        folder = self.product(path)
        try:
            dirs = sorted([{'name':p.name,'path':str(p)} for p in folder.iterdir() if p.is_dir()],key=lambda p:p['name'].lower())
        except PermissionError:
            raise StudioError('没有权限浏览这个文件夹。',403)
        return {'path':str(folder),'parent':str(folder.parent) if folder.parent!=folder else '', 'directories':dirs}

    def list_tasks(self, product_dir=''):
        tasks=[]
        for path in (self.exchange/'tasks').glob('*/task.json'):
            task=read_json(path)
            if not product_dir or Path(task['product_dir']).resolve()==Path(product_dir).resolve():
                entry = {k:task[k] for k in ('id','product_name','product_dir','created_at','delivered')}
                entry.update(variant_name=(task.get('variant') or {}).get('name'),
                             variant_group=task.get('variant_group'),
                             variant_count=len(task.get('variants') or []))
                tasks.append(entry)
        return sorted(tasks,key=lambda t:t['created_at'],reverse=True)

    def create(self, data):
        scan=self.scan(data.get('product_dir',''))
        models=scan['models']
        selected=data.get('model_file','')
        if selected and (str(Path(selected).resolve()) not in models or not selected.lower().endswith('.max')):
            raise StudioError('请选择本次扫描列出的 Max 模型。',403)
        max_file=data.get('model_max') or next((p for p in models if p.lower().endswith('.max')),'')
        if selected and selected.lower().endswith('.max'):
            max_file=str(Path(selected).resolve())
        if max_file and (str(Path(max_file).resolve()) not in models or not max_file.lower().endswith('.max')):
            raise StudioError('请选择本次扫描列出的 Max 模型。',403)
        task_id=new_id()
        task={'id':task_id,'created_at':now(),'created_by':'','product_dir':scan['product_dir'],
              'product_name':scan['product_name'],'model_max':max_file,
              'preview_source':'export','preview_fbx':str(self.task_dir(task_id)/'preview'/'preview.fbx'),
              'preview_rotation_deg':0,'reference_images':scan['reference_images'],
              'items':draft_items(),'delivered':None}
        from .calibration import initial_calibration
        task['ai_calibration'] = initial_calibration(self.config)
        with self.lock:
            self.save(task)
        return task

    def _variant_siblings(self, task):
        group = task.get('variant_group')
        if not group:
            return []
        siblings = []
        for path in sorted((self.exchange/'tasks').glob('*/task.json')):
            if path.parent.name != task['id'] and read_json(path).get('variant_group') == group:
                siblings.append(self.load(path.parent.name))
        return siblings

    def set_variants(self, task_id, data):
        variants = validate_variants(data.get('variants'))
        active_id = validate_variant_id(data.get('active'))
        by_id = {variant['id']:variant for variant in variants}
        if active_id not in by_id:
            raise StudioError('当前款式必须在识别列表里。')
        with self.lock:
            from .calibration import ACTIVE

            task = self.load(task_id)
            siblings = self._variant_siblings(task)
            members = []
            for member in [task, *siblings]:
                jobs = self.job_index.for_task(member['id'])
                if any(job['type'] == 'render' and status['state'] not in TERMINAL for job, status in jobs):
                    raise StudioError('请等同组柜子渲染结束后再重新识别。', 409)
                merged = self._merge(member['id'], jobs)['task']
                if (merged['ai_calibration']['state'] in ACTIVE
                        or any(item[mode]['state'] in ('queued', 'rendering')
                               for item in merged['items'] for mode in ('smallshot', 'final'))):
                    raise StudioError('请等同组柜子渲染或 AI 校准结束后再重新识别。', 409)
                members.append(merged)
            task, *siblings = members
            active = by_id[active_id] if len(variants) > 1 else None
            if active and any((sibling.get('variant') or {}).get('id') == active_id for sibling in siblings):
                raise StudioError('当前款式已有同组任务，请切换到对应的任务后再重新识别。', 409)
            orphan_variants = {sibling['variant']['id']: sibling['variant'] for sibling in siblings
                               if sibling.get('variant') and sibling['variant']['id'] not in by_id}
            validate_variant_names([*variants, *orphan_variants.values()])
            if active:
                task['variant_group'] = task.get('variant_group') or task_id
            self._save_variant_selection(task, variants, active)
            links, orphans = [], []
            for sibling in siblings:
                previous = sibling.get('variant')
                variant_id = previous['id'] if previous else None
                self._save_variant_selection(sibling, variants, by_id.get(variant_id, previous))
                link = {'task_id':sibling['id'], 'variant_id':variant_id}
                links.append(link)
                if previous and variant_id not in by_id:
                    orphans.append(link)
            return {'task':task, 'siblings':links, 'orphans':orphans}

    def _save_variant_selection(self, task, variants, variant):
        prior_nodes = set((task.get('variant') or {}).get('nodes', []))
        if prior_nodes != set((variant or {}).get('nodes', [])):
            for item in task['items']:
                for mode in ('smallshot', 'final'):
                    item[mode].update(state='none', job_id='')
                    item[mode].pop('job_item', None)
                    item[mode].pop('message', None)
            task['ai_calibration'].update(accepted_overrides=None, accepted_round=None)
        task.update(variants=variants, variant=variant)
        self.save(task)

    def switch_variant(self, task_id, data):
        variant_id = validate_variant_id(data.get('variant_id'))
        with self.lock:
            from .calibration import initial_calibration

            task = self.load(task_id)
            variants = task.get('variants') or []
            for member in [task, *self._variant_siblings(task)]:
                if (member.get('variant') or {}).get('id') == variant_id:
                    return {'task_id':member['id']}
            variant = next((variant for variant in variants if variant['id'] == variant_id), None)
            if variant is None:
                raise StudioError('没有找到这个款式。', 404)
            if len(variants) == 1:
                return {'task_id':task_id}

            source = self.task_dir(task_id)/'preview'
            if (self._merge(task_id)['preview']['state'] != 'done'
                    or not self._preview_files_ready(source)):
                raise StudioError('请等预览模型准备好再切换柜子', 409)
            sibling_id = new_id()
            destination = self.task_dir(sibling_id)/'preview'
            destination.mkdir(parents=True)
            for filename in ('preview.fbx', 'bbox.json'):
                shutil.copy2(source/filename, destination/filename)

            copied_fields = ('product_dir', 'product_name', 'model_max', 'reference_images',
                             'lighting', 'output_preset', 'variants', 'variant_group')
            sibling = {key:task[key] for key in copied_fields if key in task}
            sibling.update(id=sibling_id, created_at=now(), created_by='', preview_source='export',
                           preview_fbx=str(destination/'preview.fbx'), preview_rotation_deg=0,
                           variant=variant, items=draft_items(), delivered=None)
            sibling['ai_calibration'] = initial_calibration(self.config)
            sibling['ai_calibration']['enabled'] = task['ai_calibration']['enabled']
            self.save(sibling)
            return {'task_id':sibling_id}

    def rename_variant(self, task_id, data):
        variant_id = validate_variant_id(data.get('variant_id'))
        name = validate_variant_name(data.get('name'))
        with self.lock:
            task = self.load(task_id)
            members = [task, *self._variant_siblings(task)]
            by_id = {variant['id']:variant for variant in task.get('variants') or []}
            for member in members:
                if member.get('variant'):
                    by_id.setdefault(member['variant']['id'], member['variant'])
            if variant_id not in by_id:
                raise StudioError('没有找到这个款式。', 404)
            by_id[variant_id] = {**by_id[variant_id], 'name':name}
            validate_variant_names(by_id.values())
            for member in members:
                member['variants'] = [{**variant, 'name':name} if variant['id'] == variant_id else variant
                                      for variant in member.get('variants') or []]
                if (member.get('variant') or {}).get('id') == variant_id:
                    member['variant']['name'] = name
                self.save(member)
            return task

    def add_lighting_image(self, task_id, no, content):
        task = self.load(task_id)
        try:no = int(no)
        except (ValueError, TypeError):raise StudioError('光向示意图编号或路径无效。')
        item = next((i for i in task['items'] if i['no'] == no), None)
        if item is None:raise StudioError('没有这个清单编号。',404)
        if not item['enabled'] or not lighting_active(merged_lighting(task, item)):
            raise StudioError('此条目的光方向未生效。',409)
        if not 0 < len(content) <= MAX_LIGHTING_BYTES:raise StudioError('光向示意图不能超过 20 MB。',413)
        if not content.startswith(b'\x89PNG\r\n\x1a\n'):raise StudioError('光向示意图只接受 PNG。',415)
        from PIL import Image
        import io
        try:
            with Image.open(io.BytesIO(content)) as image:
                if image.format != 'PNG':raise ValueError()
                image.verify()
        except Exception:raise StudioError('光向示意图不是有效的 PNG。',415)
        folder = self.task_dir(task_id)/'lighting'
        if not inside(folder, self.task_dir(task_id)):raise StudioError('光向示意图编号或路径无效。',403)
        folder.mkdir(exist_ok=True)
        target = folder/f'{no}.png'
        if not inside(target, folder):raise StudioError('光向示意图编号或路径无效。',403)
        temp = folder/(uuid.uuid4().hex+'.tmp')
        try:
            temp.write_bytes(content)
            with self.lock:
                latest = self.load(task_id)
                latest_item = next((i for i in latest['items'] if i['no'] == no), None)
                if latest_item is None:
                    raise StudioError('没有这个清单编号。', 404)
                if not latest_item['enabled'] or not lighting_active(merged_lighting(latest, latest_item)):
                    raise StudioError('此条目的光方向未生效。', 409)
                backup(target)
                replace_retry(temp, target)
        finally:
            if temp.exists():
                try:temp.unlink()
                except OSError:pass
        return {'no':no, 'file':str(target)}

    def _stage_reference_file(self, task_id, name, content):
        if not (self.task_dir(task_id)/'task.json').is_file():
            raise StudioError('没有找到这个任务。', 404)
        name = str(name).replace('\\', '/').split('/')[-1]
        suffix = Path(name).suffix.lower()
        validate_reference(name, content)
        # 去掉 Windows 文件名中的无效字符，使用独占创建避免同名覆盖。
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).rstrip(' .')
        if name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
            name = '_' + name
        folder = self.task_dir(task_id)/'references'
        if not inside(folder, self.task_dir(task_id)):
            raise StudioError('参考图目录越界。',403)
        folder.mkdir(exist_ok=True)
        stem = Path(name).stem
        index = 1
        while True:
            target = folder/name
            if not inside(target, folder):
                raise StudioError('参考图路径越界。',403)
            try:
                with target.open('xb') as stream:
                    stream.write(content)
                return str(target.resolve())
            except FileExistsError:
                index += 1
                name = f'{stem} ({index}){suffix}'

    def add_reference(self, task_id, name, content):
        saved = self._stage_reference_file(task_id, name, content)
        with self.lock:
            task = self.load(task_id)
            task['reference_images'].append(saved)
            self.save(task)
            return task

    def add_reference_paths(self, task_id, paths):
        if not (self.task_dir(task_id)/'task.json').is_file():
            raise StudioError('没有找到这个任务。', 404)
        if not isinstance(paths, list) or not 1 <= len(paths) <= 100:
            raise StudioError('请选择 1—100 张参考图。')
        # 先检查整批，避免后面的错误导致前面的图片重复加入。
        images = []
        for value in paths:
            path = Path(value)
            if not path.is_absolute() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                raise StudioError('请选择图片的完整路径。',415)
            with path.open('rb') as stream:
                validate_reference(path.name, stream.read(12), path.stat().st_size)
            images.append(path)
        saved_paths = []
        for path in images:
            with path.open('rb') as stream:
                content = stream.read(MAX_REFERENCE_BYTES + 1)
            saved_paths.append(self._stage_reference_file(task_id, path.name, content))
        with self.lock:
            task = self.load(task_id)
            task['reference_images'].extend(saved_paths)
            self.save(task)
            return task

    def update(self, task_id, data):
        with self.lock:
            return self._update(task_id, data)

    def _update(self, task_id, data):
        task=self.merge(task_id)['task']
        from .calibration import ACTIVE
        active = task['ai_calibration']['state'] in ACTIVE
        stash = list(task.get('removed_items') or [])
        old_lighting = {i['item_id']:effective_lighting(merged_lighting(task, i)) for i in task['items']}
        for entry in stash:
            old_lighting.setdefault(entry['item_id'], entry.get('effective_lighting')
                                    or effective_lighting(merged_lighting(task, entry['item'])))
        moves=[]; discard=[]
        if 'lighting' in data:
            lighting = validate_lighting(data['lighting'])
            if active and lighting != task['lighting']:raise StudioError(CALIBRATION_EDIT_CONFLICT,409)
            task['lighting'] = lighting
        if 'ai_calibration' in data:
            enabled = data['ai_calibration'].get('enabled')
            if not isinstance(enabled, bool):raise StudioError('AI 校准开关无效。')
            if active and enabled != task['ai_calibration']['enabled']:
                raise StudioError('请等当前校准结束后再切换开关。',409)
            task['ai_calibration']['enabled'] = enabled
        if 'items' in data:
            incoming=data['items']
            if not isinstance(incoming,list):
                raise StudioError('清单需要保留 1–100 项。')
            old=task['items']
            structural=len(old)!=len(incoming) or any(x.get('no')!=i+1 for i,x in enumerate(incoming))
            for i,x in enumerate(incoming):
                if i<len(old) and (any(x.get(k)!=old[i].get(k) for k in ('label','view'))
                                   or ('item_id' in x and x['item_id']!=old[i]['item_id'])):
                    structural=True
            if active and (structural or any(bool(x.get('enabled', True)) != prior['enabled']
                                             for x, prior in zip(incoming, old))):
                raise StudioError(CALIBRATION_EDIT_CONFLICT,409)
            if not 1<=len(incoming)<=100:
                raise StudioError('清单需要保留 1–100 项。')
            if structural and (task_id in self._delivering
                               or any(x[s]['state'] in ('queued','rendering') for x in old for s in ('smallshot','final'))
                               or any(job['type']=='render' and status['state'] not in TERMINAL
                                      for job, status in self.job_index.for_task(task_id))):
                raise StudioError('请等当前小样或成品任务结束后再调整清单顺序。',409)
            normalized=[]; used_ids=set(); renumbered={}
            old_by_id={item['item_id']:item for item in old}
            stashed={entry['item_id']:entry['item'] for entry in stash}
            folder=self.task_dir(task_id)
            edited_at=now()
            for i,x in enumerate(incoming):
                no=i+1
                if x.get('view') not in ('main','front','side','top','back','custom') or x.get('orientation') not in ('portrait','landscape'):
                    raise StudioError('视角类型或画幅无效。')
                part_values=x.get('parts',{})
                if not isinstance(part_values,dict) or not all(isinstance(v,(int,float)) and math.isfinite(v) and 0<=v<=1 for v in part_values.values()):
                    raise StudioError('开合值需要在 0–100% 之间。')
                item={'no':no,'label':str(x.get('label','自定'))[:80],'view':x['view'],'orientation':x['orientation'],
                      'enabled':bool(x.get('enabled',True)), 'camera':validate_camera(x['camera']),
                      'lighting':validate_lighting(x['lighting']) if x.get('lighting') is not None else None,
                      'parts':part_values,'render_open_state':False,'note':str(x.get('note',''))[:2000]}
                restored=False
                if structural:
                    # 增删、排序：按 item_id 对回原条目（或刚删掉、可撤销的条目），成果跟着条目走。
                    identity=x.get('item_id')
                    if not isinstance(identity,str) or identity in used_ids:identity=None
                    prior=old_by_id.get(identity)
                    if prior is None and identity in stashed:
                        prior=stashed[identity]; restored=True
                    if prior is None:identity=uuid.uuid4().hex
                else:
                    prior=old[i]; identity=prior['item_id']
                if active and item['lighting'] != prior['lighting']:
                    raise StudioError(CALIBRATION_EDIT_CONFLICT,409)
                item['item_id'] = identity
                used_ids.add(identity)
                if prior and item['camera'] == validate_camera(prior['camera']):
                    item['camera'] = prior['camera']
                changed=not prior or any(item[k]!=prior[k] for k in ('camera','orientation'))
                if changed or item['parts'] != prior['parts']:
                    item['appearance_updated_at'] = edited_at
                elif 'appearance_updated_at' in prior:
                    item['appearance_updated_at'] = prior['appearance_updated_at']
                for mode,sub in (('smallshot','smallshots'),('final','final')):
                    item[mode]={'state':'none','file':f'{sub}/{no}.jpg','job_id':''}
                    if mode=='final':item[mode]['mask']=f'final/{no}_遮罩_ID.jpg'
                    if not changed:item[mode]=renumbered_result(prior[mode],prior['no'],no,sub)
                if prior and not restored:
                    renumbered[prior['no']]=no
                if restored:
                    discard.append(folder/'removed'/identity)
                    moves += [(folder/'removed'/identity/f'{sub}{suffix}', folder/sub/f'{no}{suffix}') for sub,suffix in RESULT_FILES]
                elif prior and prior['no']!=no:
                    moves += [(folder/sub/f"{prior['no']}{suffix}", folder/sub/f'{no}{suffix}') for sub,suffix in RESULT_FILES]
                normalized.append(item)
            # 删掉的条目连同成果暂存到 removed/，撤销时按 item_id 恢复。
            for gone in old:
                if gone['item_id'] in used_ids:continue
                moves += [(folder/sub/f"{gone['no']}{suffix}", folder/'removed'/gone['item_id']/f'{sub}{suffix}')
                          for sub,suffix in RESULT_FILES]
                stash=[e for e in stash if e['item_id']!=gone['item_id']]
                stash.append({'item_id':gone['item_id'],'removed_at':edited_at,
                              'effective_lighting':old_lighting[gone['item_id']],'item':copy.deepcopy(gone)})
            stash=[e for e in stash if e['item_id'] not in used_ids]
            discard += [folder/'removed'/e['item_id'] for e in stash[:-REMOVED_KEEP]]
            stash=stash[-REMOVED_KEEP:]
            if stash or 'removed_items' in task:
                task['removed_items']=stash
            overrides=task['ai_calibration'].get('accepted_overrides')
            if structural and isinstance(overrides,dict) and isinstance(overrides.get('items'),dict):
                # 校准认可的单张调整按编号记，跟着条目改编号；删掉的条目去掉。
                overrides['items']={str(renumbered[int(k)]):v for k,v in overrides['items'].items()
                                    if str(k).isdigit() and int(k) in renumbered}
            task['items']=normalized
        lighting_changed = False
        for entry in task['items']:
            if entry['item_id'] in old_lighting and old_lighting[entry['item_id']] != effective_lighting(merged_lighting(task, entry)):
                lighting_changed |= entry['enabled']
                entry['appearance_updated_at'] = now()
                for mode in ('smallshot','final'):
                    entry[mode].update(state='none',job_id='')
                    entry[mode].pop('job_item', None)
        if lighting_changed:
            task['ai_calibration'].update(accepted_overrides=None, accepted_round=None)
        if 'output_preset' in data:
            preset=data['output_preset']
            if preset not in output_presets(self.config):
                raise StudioError('输出尺寸无效。')
            if preset!=task.get('output_preset'):
                if any(x['final']['state'] in ('queued','rendering') for x in task['items']):
                    raise StudioError('请等当前渲染结束后再改输出尺寸。',409)
                task['output_preset']=preset
                # 尺寸变了，已出的成品不再对应，需要重新渲染
                for entry in task['items']:
                    entry['final'].update(state='none',job_id='')
                    entry['final'].pop('job_item', None)
        if 'preview_rotation_deg' in data:
            angle=data['preview_rotation_deg']
            if not isinstance(angle,(int,float)) or angle%90:
                raise StudioError('模型转动量必须为 90° 的整倍数。')
            task['preview_rotation_deg']=angle%360
        moved=move_files(moves) if moves else None
        try:
            self.save(task)
        except BaseException:
            if moved:undo_moves(*moved)
            raise
        for path in discard:
            if inside(path, self.task_dir(task_id)/'removed'):
                shutil.rmtree(path, ignore_errors=True)
        return task

    def draft(self, task_id, data):
        task=self.load(task_id)
        if data.get('initial'):
            initial = draft_items()
            comparable = lambda items: [{k:v for k,v in item.items() if k not in ('item_id', 'lighting_text')}
                                        for item in items]
            if comparable(task['items']) != comparable(initial):
                return task
        if task['ai_calibration']['state'] in ('rendering','thinking'):
            if data.get('initial'):return task
            raise StudioError(CALIBRATION_EDIT_CONFLICT,409)
        size=data.get('size_mm')
        if not isinstance(size,list) or len(size)!=3 or not all(isinstance(v,(int,float)) and math.isfinite(v) and 0<v<1e8 for v in size):
            raise StudioError('需要有效的模型毫米尺寸。')
        parts=data.get('parts',[])
        if not isinstance(parts,list) or not all(isinstance(p,dict) and isinstance(p.get('name'),str) for p in parts):
            raise StudioError('部件信息无效。')
        if any(x[s]['state'] in ('queued','rendering') for x in task['items'] for s in ('smallshot','final')):
            raise StudioError('请等渲染结束后再恢复建议。',409)
        suggested = draft_items(size,parts)
        visible_items = [{k: v for k, v in item.items() if k not in ('item_id', 'appearance_updated_at')}
                         for item in task['items']]
        if suggested == visible_items:
            return task
        task['items']=suggested
        edited_at=now()
        for item in task['items']:
            item['appearance_updated_at'] = edited_at
        self.save(task)
        return task

    def jobs_for(self, task_id):
        return [job for job, _ in self.job_index.for_task(task_id)]

    # ── 删除任务：整个任务文件夹移到 exchange/trash/（可撤销）；回收站里超过 TRASH_KEEP_DAYS 天的在启动时清掉。
    # 产品文件夹和已入库的 sku 文件都不动。
    TRASH_KEEP_DAYS = 30

    def _pending_jobs(self, task_id):
        return [job['job_id'] for job, status in self.job_index.for_task(task_id)
                if status.get('state') not in TERMINAL]

    def delete_task(self, task_id):
        from .calibration import ACTIVE
        task = self.load(task_id)
        running = any(x[s]['state'] in ('queued', 'rendering') for x in task['items'] for s in ('smallshot', 'final'))
        if task_id in self._delivering or running or task['ai_calibration']['state'] in ACTIVE or self._pending_jobs(task_id):
            raise StudioError('这个任务还在出图或校准，等它结束再删除。', 409)
        source = self.task_dir(task_id)
        trash = self.exchange/'trash'
        trash.mkdir(exist_ok=True)
        target = trash/task_id
        if target.exists():
            target = trash/f'{task_id}~{uuid.uuid4().hex[:6]}'
        os.replace(source, target)
        return {'deleted': task_id, 'name': task['product_name']+(' · '+task['variant']['name'] if task.get('variant') else '')}

    def restore_task(self, task_id):
        self.task_dir(task_id)          # 校验任务号
        trash = self.exchange/'trash'
        candidates = sorted((p for p in trash.glob(task_id+'*') if p.is_dir() and (p.name == task_id or p.name.startswith(task_id+'~'))),
                            key=lambda p: p.stat().st_mtime, reverse=True) if trash.is_dir() else []
        if not candidates:
            raise StudioError('回收站里没有这个任务。', 404)
        target = self.task_dir(task_id)
        if target.exists():
            raise StudioError('同名任务已经存在，不能恢复。', 409)
        os.replace(candidates[0], target)
        return {'task': self.load(task_id)}

    def purge_trash(self, now_ts=None):
        trash = self.exchange/'trash'
        if not trash.is_dir():
            return 0
        limit = (now_ts or time.time()) - self.TRASH_KEEP_DAYS*86400
        removed = 0
        for path in trash.iterdir():
            try:
                if path.is_dir() and path.stat().st_mtime < limit and inside(path, trash):
                    shutil.rmtree(path)
                    removed += 1
            except OSError:
                continue
        return removed

    @staticmethod
    def _preview_files_ready(folder):
        return all(inside(folder/filename, folder.parent) and (folder/filename).is_file()
                   for filename in ('preview.fbx', 'bbox.json'))

    def preview(self, task_id, data):
        task=self.load(task_id)
        if data.get('source')=='file':
            raise StudioError('只使用产品的 .max 模型，不再导入 FBX。',400)
        if not task['model_max'] or not Path(task['model_max']).is_file():
            raise StudioError('产品文件夹里没有 .max 模型。')
        prior=[(j, s) for j, s in self.job_index.for_task(task_id) if j['type']=='export_preview']
        base=self.task_dir(task_id)/'preview'
        if not prior and task.get('variant_group') and self._preview_files_ready(base):
            return {'job':None,'task':task}
        if prior:
            latest, latest_status = prior[-1]
            if latest_status.get('state') != 'failed':
                return {'job':latest,'task':task}
        job_id=new_id()
        job={'type':'export_preview','job_id':job_id,'task_id':task_id,'created_at':now(),
             'max_file':task['model_max'],'out_fbx':str(base/'preview.fbx'),'out_bbox':str(base/'bbox.json')}
        self.write_job(job)
        task.update(preview_source='export',preview_fbx=job['out_fbx'],preview_rotation_deg=0)
        self.save(task)
        return {'job':job,'task':task}

    def submit(self, task_id, data):
        with self.lock:
            return self._submit(task_id, data)

    def _submit(self, task_id, data):
        from .calibration import ACTIVE
        jobs=self.job_index.for_task(task_id)
        task=self._merge(task_id,jobs)['task']; mode=data.get('mode')
        if mode not in ('smallshot','final'):raise StudioError('渲染类型无效。')
        if task['ai_calibration']['state'] in ACTIVE:
            raise StudioError(CALIBRATION_RENDER_CONFLICT,409)
        if not task['model_max'] or not Path(task['model_max']).is_file():raise StudioError('正式出图需要产品的 .max 模型。')
        selected=[x for x in task['items'] if x['enabled']]
        if not selected:raise StudioError('请先启用至少一项。')
        if any(job.get('mode') == mode and status['state'] not in TERMINAL for job, status in jobs):
            raise StudioError('这批任务已经在排队或渲染。',409)
        if mode=='smallshot':
            if self.config.get('calibration_auto', False) and task['ai_calibration']['enabled']:
                return self.calibration.start(task)
        mat_mod = _resolve_materials()
        overrides = (mat_mod.effective_overrides(task) if mat_mod is not None
                     else _fallback_effective_overrides(task))
        return self._submit_render(task, mode, overrides)

    def _submit_render(self, task, mode, overrides=None, round_no=None, job_id=None):
        task_id=task['id']
        selected=[x for x in task['items'] if x['enabled']]
        job_id=job_id or new_id(); folder='smallshots' if mode=='smallshot' else 'final'
        try:
            lighting_config=read_json(self.root/'config.json')
        except FileNotFoundError:
            # 允许用内存配置创建独立 Studio 实例。
            lighting_config=self.config
        lighting_config=render_lighting_config(lighting_config)
        job={'type':'render','job_id':job_id,'task_id':task_id,'created_at':now(),'mode':mode,
             'max_file':task['model_max'],'template_max':str(self.template_path()),
             'template_anchor':self.config.get('template_anchor'),
             'render_preset_rps':str((self.root/self.config['render_preset_rps']).resolve()) if self.config['render_preset_rps'] else None,
             'time_limit_sec':self.config['smallshot_time_limit_sec'] if mode=='smallshot' else None,'items':[],
             'overrides':protect_light_overrides(overrides,task),'want_manifest':round_no is not None,
             **lighting_config}
        if task.get('variant'):
            job.update(keep_nodes=task['variant']['nodes'], variant_name=task['variant']['name'])
        output_folder=self.task_dir(task_id)/folder
        if round_no is not None:
            calibration_dir=self.task_dir(task_id)/'calibration'/f'round-{round_no}'
            calibration_dir.mkdir(parents=True,exist_ok=True)
            job.update(calibration_round=round_no,calibration_dir=str(calibration_dir))
            output_folder=calibration_dir/'smallshots'
        presets=output_presets(self.config)
        if mode=='final' and task.get('output_preset') in presets:
            sizes=presets[task['output_preset']]
        else:
            sizes=self.config['smallshot_size' if mode=='smallshot' else 'final_size']
        for item in selected:
            no=item['no']; width,height=sizes[item['orientation']]
            light = effective_lighting(merged_lighting(task, item))
            job['items'].append({'lighting':light,'lighting_text':lighting_text(light),
                                 'no':no,'width':width,'height':height,'camera':validate_camera(item['camera']),
                                 'out_image':str(output_folder/f'{no}.jpg'),
                                 'out_mask':str(self.task_dir(task_id)/folder/f'{no}_遮罩_ID.jpg') if mode=='final' else None})
            item[mode].update(state='queued',job_id=job_id,file=str((output_folder/f'{no}.jpg').relative_to(self.task_dir(task_id))))
            item[mode].pop('job_item', None)
        self.write_job(job)
        task.update(lighting_config)
        self.save(task)
        return job

    def merge(self, task_id):
        with self.lock:
            return self._merge(task_id)

    def describe(self, task_id):
        runner = self.runner_status()
        with self.lock:
            jobs = self.job_index.for_task(task_id)
            payload = self._merge(task_id, jobs)
            progress = self.job_index.progress(payload['task'], payload['preview'], jobs)
            stable = {**payload, 'progress': {k: v for k, v in progress.items() if k != 'eta_sec'}}
            signature = hashlib.sha256(json.dumps(
                stable, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
        payload.update(runner=runner, progress=progress, signature=signature)
        return payload

    def _merge(self, task_id, jobs=None):
        from .calibration import scene_materials

        task=self.load(task_id); before=copy.deepcopy(task); statuses=[]
        if jobs is None:
            jobs=self.job_index.for_task(task_id)
        preview={'state':'none','message':'','bbox':None}
        for job, status in jobs:
            statuses.append({'job_id':job['job_id'],'type':job['type'],'mode':job.get('mode'),**status})
            calibration=task['ai_calibration']
            if (calibration['state']=='rendering' and calibration['rounds']
                    and calibration['rounds'][-1]['job_id']==job['job_id'] and status['state']=='failed'):
                calibration['state']='failed'
                calibration['rounds'][-1]['error']='AI 校准失败，可直接用当前小样。本轮小样失败：'+status.get('message','请检查执行器日志。')
            if job['type']=='export_preview':
                preview={'state':status['state'],'message':status.get('message',''),'bbox':None}
                continue
            mapping={i['no']:i for i in status.get('items',[])}
            for item in task['items']:
                dest=item[job['mode']]
                if dest['job_id']!=job['job_id']:continue
                job_item=dest.get('job_item')
                job_no=job_item['no'] if isinstance(job_item,dict) and job_item.get('job_id')==job['job_id'] else item['no']
                entry=mapping.get(job_no,{})
                state=entry.get('state')
                if state:dest['state']={'pending':'queued'}.get(state,state)
                elif status['state']=='failed':dest['state']='failed'
                elif status['state'] in ('queued','waiting_lock'):dest['state']='queued'
                else:dest['state']='rendering' if status['state']=='running' else 'failed'
                dest['message']=entry.get('message',status.get('message',''))
        bbox_path=self.task_dir(task_id)/'preview'/'bbox.json'
        if task['preview_source']=='file' and Path(task['preview_fbx']).is_file():preview['state']='done'
        elif preview['state']=='none' and task.get('variant_group') and self._preview_files_ready(bbox_path.parent):
            preview['state']='done'
        if bbox_path.is_file() and task['preview_source']=='export':preview['bbox']=read_json(bbox_path)
        if before!=task:self.save(task)
        task['ai_calibration'].setdefault('task_refs', [])
        materials=scene_materials(self,task,jobs)
        payload={'task':task,'preview':preview,'jobs':statuses,'task_dir':str(self.task_dir(task_id)),
                 'output_presets':output_presets(self.config),'output_preset':task.get('output_preset') or DEFAULT_OUTPUT_PRESET}
        if materials is not None:payload['scene_materials']=materials
        return payload

    def allowed_file(self, path, task_id=''):
        target=(Path(path) if Path(path).is_absolute() else self.root/path).resolve()
        roots=[self.root/'web',self.exchange,self.root/'refs']
        if task_id:
            task=self.load(task_id); product=Path(task['product_dir'])
            roots += [self.product_root(product,n) for n in ('参考图','sku')]
            prod_key = str(product.resolve())
            with self._scan_lock:
                known_models = self.scanned_models.get(prod_key)
            if known_models is None:
                self.scan(str(product))
                with self._scan_lock:
                    known_models = self.scanned_models.get(prod_key, set())
            if str(target) in known_models:
                if not inside(target, product):raise StudioError('禁止读取这个路径。',403)
                if not target.is_file():raise StudioError('文件不存在。',404)
                return target
            # 任意层参考图只授权扫描所得图片，不放开产品根目录。
            if str(target) in task.get('reference_images', []) and inside(target, product) and '参考图' in target.relative_to(product).parts[:-1]:
                if not target.is_file():raise StudioError('文件不存在。',404)
                return target
        if not any(inside(target,root) for root in roots):raise StudioError('禁止读取这个路径。',403)
        if not target.is_file():raise StudioError('文件不存在。',404)
        return target

    def thumb_file(self, source, width):
        """按需生成并缓存 WebP 缩略图（放在 exchange/.thumbs，不碰 C 盘）。
        原图不是有效图片或 Pillow 失败时回退到原文件。"""
        source = Path(source)
        if source.suffix.lower() not in IMAGES:
            return source
        try:
            size = int(width)
        except (TypeError, ValueError):
            return source
        if size <= 0:
            return source
        size = max(THUMB_MIN, min(THUMB_MAX, size))
        try:
            stat = source.stat()
            resolved = source.resolve()
        except OSError:
            return source
        key = hashlib.sha256(f'{resolved}\0{stat.st_mtime_ns}\0{stat.st_size}\0{size}'.encode('utf-8')).hexdigest()[:32]
        thumbs = self.exchange / '.thumbs'
        target = thumbs / f'{key}.webp'
        try:
            if target.is_file():
                now_ts = time.time()
                if now_ts - target.stat().st_mtime > 86400:
                    os.utime(target, (now_ts, now_ts))
                return target
            from PIL import Image, ImageOps
            with Image.open(source) as image:
                image.seek(0)
                image = ImageOps.exif_transpose(image)
                if max(image.size) <= size and source.suffix.lower() == '.webp':
                    return source
                if image.mode not in ('RGB', 'RGBA'):
                    image = image.convert('RGBA' if 'A' in image.getbands() or 'transparency' in image.info else 'RGB')
                image.thumbnail((size, size), Image.LANCZOS)
                thumbs.mkdir(parents=True, exist_ok=True)
                temp = thumbs / f'.{key}.{uuid.uuid4().hex[:8]}.tmp'
                try:
                    image.save(temp, 'WEBP', quality=84, method=4)
                    replace_retry(temp, target)
                finally:
                    if temp.exists():
                        try:temp.unlink()
                        except OSError:pass
            return target
        except Exception:
            return source

    def purge_thumbs(self, now_ts=None):
        thumbs = self.exchange / '.thumbs'
        if not thumbs.is_dir():
            return 0
        limit = (now_ts or time.time()) - THUMB_KEEP_DAYS * 86400
        removed = 0
        for path in thumbs.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < limit and inside(path, thumbs):
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def delivery_plan(self, task_id):
        task=self.merge(task_id)['task']; product=Path(task['product_dir'])
        sku,custom=self.delivery_dir(product); files=[]; selected=[]
        if custom and not sku.is_dir():raise StudioError('保存文件夹不存在了，请回首页重新选择保存位置。',409)
        light_root=self.product_root(product,'光向参考')
        if not inside(light_root/'旧版',light_root):raise StudioError('光向参考旧版目录越界。',403)
        if not inside(sku/'旧版',sku):
            raise StudioError('旧版目录指向了 sku 外部，不能入库。',403)
        variant = task.get('variant')
        manifest_name = f"白底清单_{variant['name']}.json" if variant else '白底清单.json'
        manifest = sku/manifest_name
        if not inside(manifest, sku):
            raise StudioError('入库路径越界。',403)
        if task.get('variant_group') and manifest.is_file():
            try:
                records = read_json(manifest)
            except (ValueError, UnicodeError):
                records = None
            if isinstance(records, list):
                owners = {entry.get('task_id') for entry in records
                          if isinstance(entry, dict) and isinstance(entry.get('task_id'), str)}
                if any(sibling['id'] in owners for sibling in self._variant_siblings(task)):
                    raise StudioError('该款式名已有同组任务的入库记录，请更换款式名再入库。',409)
        for item in task['items']:
            if not item['enabled'] or item['final']['state']!='done':continue
            selected.append(item)
            no = item['no']
            stem = delivery_stem(task, no)
            for suffix in ('.jpg', '_遮罩_ID.jpg'):
                source=self.task_dir(task_id)/'final'/f'{no}{suffix}'
                name=f'{stem}{suffix}'
                if not inside(source,self.task_dir(task_id)/'final'):
                    raise StudioError('成品源文件路径越界。',403)
                if not source.is_file():raise StudioError(f'成品缺少 {source.name}，请补齐后再入库。',409)
                files.append({'name':name,'source':str(source),'destination':str(sku/name),
                              'action':'archive' if (sku/name).exists() else 'new'})
            if lighting_active(merged_lighting(task, item)):
                source=self.task_dir(task_id)/'lighting'/f'{no}.png'
                if not inside(source,self.task_dir(task_id)/'lighting'):raise StudioError('光向示意图编号或路径无效。',403)
                if not source.is_file():raise StudioError('缺少光向示意图，请打开预览并等待示意图保存。',409)
                target=light_root/f'{stem}.png'
                files.append({'name':'光向参考/'+target.name,'source':str(source),'destination':str(target),
                              'action':'archive' if target.exists() else 'new'})
        if not selected:raise StudioError('还没有启用且完成的成品。',409)
        files.append({'name':manifest_name,'source':None,'destination':str(manifest),
                      'action':'archive' if manifest.exists() else 'new'})
        for f in files:
            if not any(inside(f['destination'],r) for r in (sku,light_root)):raise StudioError('入库路径越界。',403)
            p=Path(f['destination']); f['existing_stamp']=[p.stat().st_size,p.stat().st_mtime_ns] if p.exists() else None
            if f['source']:
                p=Path(f['source']);f['source_stamp']=[p.stat().st_size,p.stat().st_mtime_ns]
        token=hashlib.sha256(json.dumps([task,files],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        return {'task_id':task_id,'sku_dir':str(sku),'files':files,'token':token}

    def deliver(self, task_id, token):
        with self.lock:
            if task_id in self._delivering:
                raise StudioError('该任务正在入库，请稍候。', 409)
            plan = self.delivery_plan(task_id)
            if token != plan['token']:
                raise StudioError('入库清单已变化，请重新查看并确认。', 409)
            task = self.load(task_id)
            delivered_at = now()
            records = []
            for item in task['items']:
                if item['enabled'] and item['final']['state'] == 'done':
                    light = merged_lighting(task, item)
                    light['enabled'] = lighting_active(light)
                    records.append({
                        'no': item['no'], 'label': item['label'], 'view': item['view'],
                        'orientation': item['orientation'], 'lighting': light,
                        'lighting_text': lighting_text(light), 'camera': item['camera'],
                        'model_max': task['model_max'], 'task_id': task_id, 'delivered_at': delivered_at,
                    })
                    if task.get('variant'):
                        stem = delivery_stem(task, item['no'])
                        records[-1].update(image_file=f'{stem}.jpg', mask_file=f'{stem}_遮罩_ID.jpg',
                                           lighting_file=f'光向参考/{stem}.png' if light['enabled'] else None)
            self._delivering.add(task_id)

        try:
            sku = Path(plan['sku_dir'])
            sku.mkdir(parents=True, exist_ok=True)
            light_root = self.product_root(Path(task['product_dir']), '光向参考')
            has_light = any(inside(f['destination'], light_root) for f in plan['files'])
            sku_staging = sku / f'.staging-{uuid.uuid4().hex}'
            light_staging = (light_root / f'.staging-{uuid.uuid4().hex}') if has_light else None
            sku_staging.mkdir(parents=False, exist_ok=False)
            if light_staging is not None:
                light_root.mkdir(parents=True, exist_ok=True)
                light_staging.mkdir(parents=False, exist_ok=False)
            try:
                # 1) 锁外先将成图、遮罩和新的白底清单.json 写入 .staging-<uuid>/
                staged_pairs = []
                for f in plan['files']:
                    target = Path(f['destination'])
                    is_light = inside(target, light_root)
                    stage_dir = light_staging if is_light else sku_staging
                    staged = stage_dir / target.name
                    if f['source']:
                        shutil.copy2(f['source'], staged)
                    else:
                        atomic_json(staged, records)
                    staged_pairs.append((staged, target, is_light))

                # 2) 将 sku/（及光向参考/）下同名旧文件移入 旧版/<YYYYMMDD-HHMMSS>/，不生成任何 .prev
                archive_time = datetime.now()
                archive = sku / '旧版' / archive_time.strftime('%Y%m%d-%H%M%S')
                while archive.exists():
                    archive_time += timedelta(seconds=1)
                    archive = sku / '旧版' / archive_time.strftime('%Y%m%d-%H%M%S')
                light_archive = light_root / '旧版' / archive.name
                while light_archive.exists():
                    archive_time += timedelta(seconds=1)
                    light_archive = light_root / '旧版' / archive_time.strftime('%Y%m%d-%H%M%S')

                archived_moves = []
                placed_targets = []
                try:
                    for staged, target, is_light in staged_pairs:
                        if target.exists():
                            dest_archive = light_archive if is_light else archive
                            dest_archive.mkdir(parents=True, exist_ok=True)
                            archived_path = dest_archive / target.name
                            replace_retry(target, archived_path)
                            archived_moves.append((target, archived_path))
                    # 3) 将 staging 里的文件原子 os.replace 进 sku/（及光向参考/）
                    for staged, target, _ in staged_pairs:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        replace_retry(staged, target)
                        placed_targets.append((staged, target))
                except BaseException:
                    for staged, target in reversed(placed_targets):
                        try:os.replace(target, staged)
                        except OSError:pass
                    for target, archived_path in reversed(archived_moves):
                        try:os.replace(archived_path, target)
                        except OSError:pass
                    for folder in (archive, light_archive):
                        try:
                            if folder.is_dir() and not any(folder.iterdir()):
                                folder.rmdir()
                        except OSError:
                            pass
                    raise
            finally:
                shutil.rmtree(sku_staging, ignore_errors=True)
                if light_staging is not None:
                    shutil.rmtree(light_staging, ignore_errors=True)

            with self.lock:
                latest_task = self.load(task_id)
                latest_task['delivered'] = {
                    'at': delivered_at,
                    'sku_dir': str(sku),
                    'files': [f['name'] for f in plan['files']],
                    'archive_dir': str(archive) if archive.exists() else None,
                }
                self.save(latest_task)
                return latest_task['delivered']
        finally:
            with self.lock:
                self._delivering.discard(task_id)
