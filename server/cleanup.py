"""已入库任务的交换目录统计与中间文件清理。"""
import hashlib
import os
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

from .core import StudioError, atomic_json, inside, now, read_json
from .jobs import TERMINAL, epoch

OUTPUT_DIRS = ('smallshots', 'calibration', 'final', 'references', 'lighting', 'calibration_refs')
STAGED_PREFIX = '.cleanup-'
DAY = 24*60*60


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
            self.stopping.wait(DAY)

    def folders(self):
        if ordinary_dir(self.tasks) and inside(self.tasks, self.studio.exchange):
            for folder in self.tasks.iterdir():
                if ordinary_dir(folder) and inside(folder, self.tasks):
                    yield folder

    def read_task(self, folder):
        path = folder/'task.json'
        if path.is_symlink():
            return None
        try:
            task = read_json(path)
            if isinstance(task, dict) and task.get('id') == folder.name and self.studio.task_dir(folder.name) == folder:
                return task
        except (OSError, ValueError, StudioError):
            pass
        return None

    def outputs(self, folder):
        for name in OUTPUT_DIRS:
            original, staged = folder/name, folder/(STAGED_PREFIX+name)
            for path in (original, staged):
                if ordinary_dir(path) and inside(path, folder):
                    yield path

    def eligible(self, task, before):
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
        for folder in self.folders():
            task = self.read_task(folder)
            if task is None:
                continue
            tasks += 1
            paths = list(self.outputs(folder))
            if paths and self.eligible(task, None):
                cleanable_tasks += 1
                cleanable_bytes += sum(map(size_bytes, paths))
        return dict(exchange_bytes=size_bytes(self.studio.exchange), tasks=tasks,
                    cleanable_tasks=cleanable_tasks, cleanable_bytes=cleanable_bytes)

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
        tasks = self.studio.list_tasks(product_dir)
        for entry in tasks:
            folder = self.studio.task_dir(entry['id'])
            task = self.read_task(folder)
            if not task:
                continue
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
        return tasks

    def describe(self, task_id):
        bundle = self.studio.describe(task_id)
        task = bundle['task']
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
