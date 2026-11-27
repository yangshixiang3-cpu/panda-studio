"""已入库任务的交换目录统计与中间文件清理（含 mtime 内存缓存与超期作业归档）。"""
import copy
import hashlib
import json
import os
import shutil
import stat
import sys
import threading
import time
import uuid
from pathlib import Path

from .core import (StudioError, _fallback_scene_materials_detail, _resolve_materials,
                   atomic_json, inside, now, read_json, replace_retry)
from .jobs import TERMINAL, duration_sample, epoch, valid_job_doc

OUTPUT_DIRS = ('smallshots', 'calibration', 'final', 'references', 'lighting', 'calibration_refs')
STAGED_PREFIX = '.cleanup-'
DAY = 24*60*60
DEFAULT_JOBS_ARCHIVE_DAYS = 7


def _unlink_retry(path):
    path = Path(path)
    for attempt in range(30):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(.02)


def ordinary_dir(path):
    return path.is_dir() and not path.is_symlink() and not getattr(path, 'is_junction', lambda: False)()


def size_bytes(path):
    total = 0
    for directory, folders, files in os.walk(path, followlinks=False):
        parent = Path(directory)
        folders[:] = [name for name in folders if ordinary_dir(parent/name)]
        for name in files:
            try:
                info = (parent/name).lstat()
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
            except FileNotFoundError:
                continue
    return total


class ExchangeCleanup:
    def __init__(self, studio):
        self.studio = studio
        self.tasks = studio.exchange/'tasks'
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self._task_cache = {}
        self._task_cache_lock = threading.Lock()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name='exchange-cleanup', daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=2)

    def run(self):
        while not self.stopping.is_set():
            try:
                days = self.studio.config.get('cleanup_delivered_after_days', 30)
                if type(days) not in (int, float) or not 0 <= days < float('inf'):
                    days = 30
                self.clean(before=time.time()-days*DAY)
            except Exception as error:
                if sys.stderr is not None:
                    print(f'交换目录定期清理失败：{error}', file=sys.stderr, flush=True)
            try:
                self.archive_jobs()
            except Exception as error:
                if sys.stderr is not None:
                    print(f'历史作业定期归档失败：{error}', file=sys.stderr, flush=True)
            self.stopping.wait(DAY)

    def folders(self):
        if ordinary_dir(self.tasks) and inside(self.tasks, self.studio.exchange):
            for folder in self.tasks.iterdir():
                if ordinary_dir(folder) and inside(folder, self.tasks):
                    yield folder

    def _cached_task(self, folder):
        path = folder/'task.json'
        try:
            info = path.lstat()
        except OSError:
            with self._task_cache_lock:
                self._task_cache.pop(folder.name, None)
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
        with self._task_cache_lock:
            cached = self._task_cache.get(folder.name)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        try:
            if self.studio.task_dir(folder.name) != folder:
                return None
            task = read_json(path)
            if isinstance(task, dict) and task.get('id') == folder.name:
                with self._task_cache_lock:
                    self._task_cache[folder.name] = (stamp, task)
                return task
        except (OSError, ValueError, StudioError):
            pass
        return None

    def _prune_task_cache(self, seen_ids):
        with self._task_cache_lock:
            for stale in set(self._task_cache) - seen_ids:
                self._task_cache.pop(stale, None)

    def read_task(self, folder):
        cached = self._cached_task(folder)
        return copy.deepcopy(cached) if cached is not None else None

    def outputs(self, folder):
        for name in OUTPUT_DIRS:
            original, staged = folder/name, folder/(STAGED_PREFIX+name)
            for path in (original, staged):
                if ordinary_dir(path) and inside(path, folder):
                    yield path

    def eligible(self, task, before):
        if task.get('id') in getattr(self.studio, '_delivering', ()):
            return False
        delivered = task.get('delivered')
        if not isinstance(delivered, dict) or not delivered:
            return False
        at = epoch(delivered.get('at'))
        if before is not None and (at is None or at >= before):
            return False
        if (task.get('ai_calibration') or {}).get('state') in ('rendering', 'thinking'):
            return False
        for job, status in self.studio.job_index.for_task(task['id']):
            if status['state'] not in TERMINAL:
                return False
            if at is not None and job.get('mode') == 'final':
                finished = epoch(status.get('updated_at')) or epoch(job['created_at'])
                has_output = status['state'] == 'done' or any(i.get('state') == 'done' for i in status.get('items', []))
                if has_output and finished is not None and finished > at:
                    return False
        return True

    def storage(self):
        tasks = cleanable_tasks = cleanable_bytes = 0
        seen = set()
        for folder in self.folders():
            seen.add(folder.name)
            task = self._cached_task(folder)
            if task is None:
                continue
            tasks += 1
            paths = list(self.outputs(folder))
            if paths and self.eligible(task, None):
                cleanable_tasks += 1
                cleanable_bytes += sum(map(size_bytes, paths))
        self._prune_task_cache(seen)
        return dict(exchange_bytes=size_bytes(self.studio.exchange), tasks=tasks,
                    cleanable_tasks=cleanable_tasks, cleanable_bytes=cleanable_bytes)

    def archive_jobs(self, before=None):
        """将已结束（done/failed）且超过 jobs_archive_after_days（默认 7 天）的作业移入 exchange/jobs/archive/，
        并更新 exchange/jobs/archive/index.json（严禁在 exchange/jobs/ 顶层放置任何索引文件）。"""
        if before is None:
            days = self.studio.config.get('jobs_archive_after_days', DEFAULT_JOBS_ARCHIVE_DAYS)
            if isinstance(days, bool) or type(days) not in (int, float) or not 0 <= days < float('inf'):
                days = DEFAULT_JOBS_ARCHIVE_DAYS
            before = time.time() - days * DAY
        jobs_dir = self.studio.exchange / 'jobs'
        if not ordinary_dir(jobs_dir) or not inside(jobs_dir, self.studio.exchange):
            return {'archived_jobs': 0}
        archive_dir = jobs_dir / 'archive'
        if archive_dir.is_symlink() or getattr(archive_dir, 'is_junction', lambda: False)() or not inside(archive_dir, jobs_dir):
            raise StudioError('作业归档目录越界。', 403)
        with self.lock:
            candidates = []
            try:
                with os.scandir(jobs_dir) as entries:
                    names = [entry.name for entry in entries
                             if entry.name.endswith('.json') and not entry.name.endswith('.status.json')
                             and entry.is_file(follow_symlinks=False)]
            except OSError:
                return {'archived_jobs': 0}
            for name in sorted(names):
                if self.stopping.is_set():
                    break
                job_id = name[:-5]
                job_path = jobs_dir / name
                status_path = jobs_dir / f'{job_id}.status.json'
                if status_path.is_symlink() or not status_path.is_file():
                    continue
                try:
                    job = read_json(job_path)
                    status = read_json(status_path)
                except (OSError, ValueError):
                    continue
                if not valid_job_doc(job_id, job) or not isinstance(status, dict) or status.get('state') not in TERMINAL:
                    continue
                finished = epoch(status.get('updated_at')) or epoch(job.get('created_at'))
                if finished is None:
                    try:
                        finished = status_path.stat().st_mtime
                    except OSError:
                        continue
                if finished < before:
                    candidates.append((job_id, job_path, status_path, job, status))
            if not candidates:
                return {'archived_jobs': 0}
            archive_dir.mkdir(parents=True, exist_ok=True)
            index_path = archive_dir / 'index.json'
            jobs_map = {}
            if index_path.is_file() and not index_path.is_symlink():
                try:
                    existing = read_json(index_path)
                    if isinstance(existing, dict) and isinstance(existing.get('jobs'), dict):
                        jobs_map = dict(existing['jobs'])
                except (OSError, ValueError):
                    jobs_map = {}
            staged_ids = []
            for job_id, job_path, status_path, job, status in candidates:
                try:
                    for src, dst in ((job_path, archive_dir / f'{job_id}.json'),
                                     (status_path, archive_dir / f'{job_id}.status.json')):
                        tmp = archive_dir / f'.{job_id}.{uuid.uuid4().hex[:8]}.tmp'
                        shutil.copy2(src, tmp)
                        replace_retry(tmp, dst)
                    sample = duration_sample(job, status)
                    jobs_map[job_id] = {
                        'job_id': job_id,
                        'task_id': job['task_id'],
                        'type': job['type'],
                        'mode': job.get('mode'),
                        'created_at': job['created_at'],
                        'state': status['state'],
                        'updated_at': status.get('updated_at'),
                        'message': status.get('message', ''),
                        'sample': list(sample) if sample is not None else None,
                    }
                    staged_ids.append((job_id, job_path, status_path))
                except OSError:
                    continue
            if not staged_ids:
                return {'archived_jobs': 0}
            atomic_json(index_path, {'version': 1, 'updated_at': now(), 'jobs': jobs_map})
            archived = 0
            for job_id, job_path, status_path in staged_ids:
                try:
                    _unlink_retry(status_path)
                    _unlink_retry(job_path)
                    archived += 1
                except OSError:
                    continue
            self.studio.job_index.refresh_history()
            return {'archived_jobs': archived}

    def clean(self, before=None):
        removed_tasks = removed_bytes = 0
        with self.lock:
            for folder in self.folders():
                if self.stopping.is_set():
                    break
                if not list(self.outputs(folder)):
                    continue
                try:
                    detached, retry = self.detach(folder, before)
                    if not detached:
                        continue
                    for path in detached:
                        removed_bytes += self.remove(path, folder)
                    if retry:
                        remaining, _ = self.detach(folder, before)
                        for path in remaining:
                            removed_bytes += self.remove(path, folder)
                    if not list(self.outputs(folder)):
                        removed_tasks += 1
                except (OSError, StudioError) as error:
                    if sys.stderr is not None:
                        print(f'任务 {folder.name} 的中间文件清理未完成：{error}', file=sys.stderr, flush=True)
        return dict(cleaned_tasks=removed_tasks, cleaned_bytes=removed_bytes)

    def detach(self, folder, before):
        # 检查、移出工作路径与更新时间使用同一把锁。删除移出的文件在锁外执行。
        with self.studio.lock:
            task = self.read_task(folder)
            if task is None or not self.eligible(task, before):
                return [], False
            paths = list(self.outputs(folder))
            detached = [path for path in paths if path.name.startswith(STAGED_PREFIX)]
            moved = []
            retry = False
            try:
                for path in paths:
                    if path in detached:
                        continue
                    staged = path.with_name(STAGED_PREFIX+path.name)
                    if staged in detached:
                        retry = True
                    elif not staged.exists() and not staged.is_symlink():
                        path.rename(staged)
                        moved.append((path, staged))
                if detached or moved:
                    task['cleaned_at'] = now()
                    if not (folder/'calibration_refs').exists():
                        (task.get('ai_calibration') or {}).pop('task_refs', None)
                    atomic_json(folder/'task.json', task)
            except OSError:
                for path, staged in reversed(moved):
                    staged.rename(path)
                raise
        return detached+[staged for _, staged in moved], retry

    def remove(self, path, folder):
        if not ordinary_dir(path) or not inside(path, folder) or not inside(folder, self.tasks):
            raise StudioError('交换目录清理路径越界。', 403)
        amount = size_bytes(path)
        shutil.rmtree(path)
        return amount

    def list_tasks(self, product_dir):
        target_product = Path(product_dir).resolve() if product_dir else None
        tasks = []
        seen = set()
        for folder in self.folders():
            seen.add(folder.name)
            task = self._cached_task(folder)
            if not task:
                continue
            if not all(k in task for k in ('id', 'product_name', 'product_dir', 'created_at', 'delivered')):
                continue
            if target_product is not None:
                try:
                    if Path(task['product_dir']).resolve() != target_product:
                        continue
                except OSError:
                    continue
            entry = {
                'id': task['id'],
                'product_name': task['product_name'],
                'product_dir': task['product_dir'],
                'created_at': task['created_at'],
                'delivered': copy.deepcopy(task['delivered']),
                'variant_name': (task.get('variant') or {}).get('name'),
                'variant_group': task.get('variant_group'),
                'variant_count': len(task.get('variants') or []),
            }
            if task.get('cleaned_at'):
                entry['cleaned_at'] = task['cleaned_at']
            # 首页卡片用：启用张数、已完成数、缩略图（优先小样，避免加载几 MB 的正式图）
            items = [i for i in task.get('items', []) if i.get('enabled', True)]
            entry['item_count'] = len(items)
            entry['final_done'] = sum(1 for i in items if i.get('final', {}).get('state') == 'done')
            entry['smallshot_done'] = sum(1 for i in items if i.get('smallshot', {}).get('state') == 'done')
            entry['output_preset'] = task.get('output_preset') or 'default'
            entry['thumb'] = next((str(folder/i[mode]['file']) for mode in ('smallshot', 'final') for i in items
                                   if i.get(mode, {}).get('state') == 'done' and i[mode].get('file')
                                   and (folder/i[mode]['file']).is_file()), None)
            tasks.append(entry)
        self._prune_task_cache(seen)
        return sorted(tasks, key=lambda t: t['created_at'], reverse=True)

    def describe(self, task_id):
        bundle = self.studio.describe(task_id)
        task = bundle['task']
        jobs = self.studio.job_index.for_task(task_id)
        mat_mod = _resolve_materials()
        if mat_mod is not None:
            detail = mat_mod.scene_materials_detail(self.studio, task, jobs)
        else:
            detail = _fallback_scene_materials_detail(self.studio, task, jobs)
        bundle['materials_detail'] = detail
        if not bundle.get('scene_materials') and isinstance(detail, dict) and detail.get('materials'):
            names = [m['name'] for m in detail['materials']
                     if isinstance(m, dict) and isinstance(m.get('name'), str) and m['name']]
            if names:
                bundle['scene_materials'] = names
        mat_sig = json.dumps({'materials_detail': detail, 'scene_materials': bundle.get('scene_materials')},
                             sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        bundle['signature'] = hashlib.sha256((bundle['signature'] + mat_sig).encode('utf-8')).hexdigest()
        if not task.get('cleaned_at'):
            return bundle
        folder = self.studio.task_dir(task_id)
        outputs = {mode: [item['no'] for item in task['items']
                          if item[mode].get('file') and (folder/item[mode]['file']).is_file()]
                   for mode in ('smallshot', 'final')}
        rounds = [r['n'] for r in task['ai_calibration']['rounds']
                  if (folder/'calibration'/f"round-{r['n']}").is_dir()]
        task['reference_images'] = [p for p in task['reference_images']
                                    if not inside(p, folder/'references') or Path(p).is_file()]
        version = repr((outputs, rounds, task['reference_images']))
        bundle.update(available_outputs=outputs, available_rounds=rounds,
                      signature=hashlib.sha256((bundle['signature']+version).encode()).hexdigest())
        return bundle
