"""缓存作业及状态，按任务查询并增量维护实际处理时长（含 archive/ 历史归档无缝合并）。"""
from bisect import insort
from datetime import datetime
import os
from pathlib import Path
from statistics import median
import threading
import time


DEFAULT_SECONDS = {'preview': 8, 'smallshot': 6, 'final': 100, 'calibration': 20}
TERMINAL = ('done', 'failed')
QUEUED_STATUS = {'state': 'queued', 'items': [], 'message': '等待执行器处理。'}


def epoch(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def job_kind(job):
    return 'preview' if job['type'] == 'export_preview' else job['mode']


def valid_job_doc(job_id, job):
    return (isinstance(job, dict)
            and job.get('job_id') == job_id
            and all(isinstance(job.get(k), str) for k in ('task_id', 'created_at'))
            and (job.get('type') == 'export_preview'
                 or (job.get('type') == 'render' and job.get('mode') in ('smallshot', 'final')
                     and isinstance(job.get('items'), list))))


def duration_sample(job, status):
    if not status or status.get('state') != 'done':
        return None
    labels = [status.get('message', ''), *status.get('log', [])]
    if status.get('runner_version') == 'fake' or any(
            '假执行器' in str(line) or '开发占位' in str(line) for line in labels):
        return None
    start, end = epoch(status.get('started_at')), epoch(status.get('updated_at'))
    kind = job_kind(job)
    count = 1 if kind == 'preview' else len(job.get('items', []))
    if kind in DEFAULT_SECONDS and start is not None and end is not None and end > start and count:
        return kind, (end - start) / count
    return None


class JobIndex:
    def __init__(self, directory, read_json, *, defer_initial_scan=False):
        self.directory = Path(directory)
        self.archive_dir = self.directory / 'archive'
        self.archive_index_path = self.archive_dir / 'index.json'
        self.read_json = read_json
        self.lock = threading.RLock()
        self.jobs = {}
        self.invalid_jobs = {}
        self.by_task = {}
        self.statuses = {}
        self.samples = {}
        self.durations = {kind: [] for kind in DEFAULT_SECONDS}
        self._archive_stamp = None
        self._archive_by_task = {}
        self._archive_meta = {}
        self._archive_cache = {}
        self._archive_samples = {}
        self.stopping = threading.Event()
        self.thread = None
        if not defer_initial_scan:
            self.refresh_history()

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._watch_history, daemon=True, name='白底作业耗时')
            self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def _watch_history(self):
        while not self.stopping.wait(3):
            try:
                self.refresh_history()
            except OSError:
                continue

    def _read(self, name):
        try:
            document = self.read_json(self.directory / name)
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def _scan(self):
        metadata = {}
        if self.directory.is_dir():
            with os.scandir(self.directory) as entries:
                for entry in entries:
                    if not entry.name.endswith('.json') or not entry.is_file(follow_symlinks=False):
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    metadata[entry.name] = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        for name in self.jobs.keys() - metadata.keys():
            self._forget_job(name)
        for name in self.invalid_jobs.keys() - metadata.keys():
            del self.invalid_jobs[name]
        for name, stamp in metadata.items():
            if name.endswith('.status.json'):
                continue
            stamp_before, previous = self.jobs.get(name, (None, None))
            if stamp == stamp_before or stamp == self.invalid_jobs.get(name):
                continue
            job = self._read(name)
            if previous is not None:
                self._forget_job(name)
            if not valid_job_doc(name[:-5], job):
                if job is not None:
                    self.invalid_jobs[name] = stamp
                continue
            self.invalid_jobs.pop(name, None)
            self.jobs[name] = (stamp, job)
            self.by_task.setdefault(job['task_id'], {})[name] = job
        return metadata

    def _forget_job(self, name):
        _, job = self.jobs.pop(name)
        jobs = self.by_task[job['task_id']]
        del jobs[name]
        if not jobs:
            del self.by_task[job['task_id']]
        self.statuses.pop(name, None)
        self._remove_sample(name)
        job_id = name[:-5]
        meta = self._archive_meta.get(job_id)
        if job_id not in self._archive_samples and isinstance(meta, dict):
            self._apply_archive_sample(job_id, meta.get('sample'))

    def _remove_sample(self, name):
        if name in self.samples:
            kind, seconds = self.samples.pop(name)
            self.durations[kind].remove(seconds)

    def _remove_archive_sample(self, job_id):
        if job_id in self._archive_samples:
            kind, seconds = self._archive_samples.pop(job_id)
            self.durations[kind].remove(seconds)

    def _apply_archive_sample(self, job_id, sample):
        if (isinstance(sample, list) and len(sample) == 2 and sample[0] in self.durations
                and type(sample[1]) in (int, float) and sample[1] > 0):
            sec = float(sample[1])
            self._archive_samples[job_id] = (sample[0], sec)
            insort(self.durations[sample[0]], sec)

    def _record_sample(self, name, job, status):
        self._remove_sample(name)
        self._remove_archive_sample(job['job_id'])
        sample = duration_sample(job, status)
        if sample is not None:
            kind, seconds = sample
            self.samples[name] = (kind, seconds)
            insort(self.durations[kind], seconds)

    def _status(self, name, job, metadata):
        status_name = job['job_id'] + '.status.json'
        stamp = metadata.get(status_name)
        known, status = self.statuses.get(name, (None, None))
        if stamp != known:
            changed = self._read(status_name) if stamp is not None else None
            if stamp is None or (changed and changed.get('state') in ('waiting_lock', 'running', *TERMINAL)):
                self.statuses[name] = (stamp, changed)
                self._record_sample(name, job, changed)
                status = changed
        return status or QUEUED_STATUS

    def _sync_archive(self):
        try:
            st = self.archive_index_path.lstat()
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            if self._archive_stamp is not None:
                for job_id in list(self._archive_samples):
                    self._remove_archive_sample(job_id)
                self._archive_stamp = None
                self._archive_by_task = {}
                self._archive_meta = {}
                self._archive_cache = {}
            return
        except OSError:
            return
        if stamp == self._archive_stamp:
            return
        try:
            document = self.read_json(self.archive_index_path)
        except (OSError, ValueError):
            return
        if not isinstance(document, dict):
            return
        raw_jobs = document.get('jobs')
        jobs_meta = raw_jobs if isinstance(raw_jobs, dict) else {}
        by_task = {}
        for job_id, meta in jobs_meta.items():
            if isinstance(meta, dict) and isinstance(meta.get('task_id'), str):
                by_task.setdefault(meta['task_id'], []).append(job_id)
        for job_id in list(self._archive_samples):
            self._remove_archive_sample(job_id)
        for job_id, meta in jobs_meta.items():
            if (job_id + '.json') not in self.samples and isinstance(meta, dict):
                self._apply_archive_sample(job_id, meta.get('sample'))
        for removed in set(self._archive_cache) - set(jobs_meta):
            self._archive_cache.pop(removed, None)
        self._archive_stamp = stamp
        self._archive_meta = jobs_meta
        self._archive_by_task = by_task

    def _load_archived_job(self, job_id):
        if not isinstance(job_id, str) or not job_id or any(c in job_id for c in '/\\:'):
            return None
        cached = self._archive_cache.get(job_id)
        if cached is not None:
            return cached
        job_path = self.archive_dir / f'{job_id}.json'
        status_path = self.archive_dir / f'{job_id}.status.json'
        try:
            job = self.read_json(job_path) if job_path.is_file() else None
        except (OSError, ValueError):
            job = None
        meta = self._archive_meta.get(job_id)
        if not valid_job_doc(job_id, job):
            if isinstance(meta, dict) and valid_job_doc(job_id, meta.get('job')):
                job = meta['job']
            else:
                return None
        try:
            status = self.read_json(status_path) if status_path.is_file() else None
        except (OSError, ValueError):
            status = None
        if not isinstance(status, dict) or status.get('state') not in ('waiting_lock', 'running', *TERMINAL):
            if isinstance(meta, dict) and isinstance(meta.get('status'), dict) and meta['status'].get('state') in TERMINAL:
                status = meta['status']
            elif isinstance(meta, dict) and meta.get('state') in TERMINAL:
                status = {'job_id': job_id, 'state': meta['state'], 'items': meta.get('items', []),
                          'message': meta.get('message', ''), 'updated_at': meta.get('updated_at')}
            else:
                status = QUEUED_STATUS
        pair = (job, status)
        if job_id in self._archive_meta:
            self._archive_cache[job_id] = pair
        return pair

    def refresh_history(self):
        with self.lock:
            metadata = self._scan()
            for name, (_, job) in self.jobs.items():
                self._status(name, job, metadata)
            self._sync_archive()

    def for_task(self, task_id):
        with self.lock:
            metadata = self._scan()
            self._sync_archive()
            combined = {}
            for name, job in self.by_task.get(task_id, {}).items():
                combined[job['job_id']] = (job, self._status(name, job, metadata))
            for job_id in self._archive_by_task.get(task_id, ()):
                if job_id not in combined:
                    loaded = self._load_archived_job(job_id)
                    if loaded is not None:
                        combined[job_id] = loaded
            return sorted(combined.values(), key=lambda pair: (pair[0]['created_at'], pair[0]['job_id']))

    def get(self, job_id):
        if not isinstance(job_id, str) or not job_id or any(c in job_id for c in '/\\:'):
            return None
        with self.lock:
            metadata = self._scan()
            name = job_id + '.json'
            if name in self.jobs:
                _, job = self.jobs[name]
                return job, self._status(name, job, metadata)
            self._sync_archive()
            return self._load_archived_job(job_id)

    def seconds_per(self, kind):
        with self.lock:
            values = self.durations[kind]
            return median(values) if values else DEFAULT_SECONDS[kind]

    def render_progress(self, job, status, now):
        kind = job_kind(job)
        stage = 'rendering' if status['state'] == 'running' else 'queued'
        items = status.get('items', [])
        total = 1 if kind == 'preview' else len(job['items'])
        done = sum(i.get('state') in TERMINAL for i in items)
        per = self.seconds_per(kind)
        current = next((i for i in items if i.get('state') == 'rendering'), None)
        if kind == 'preview' and stage == 'rendering':
            started = epoch(status.get('started_at'))
        elif current is not None:
            started = epoch(current.get('started_at') or status.get('updated_at'))
        else:
            started = None
        elapsed = min(per, max(0, now - started)) if started is not None else 0
        return {'kind': kind, 'stage': stage, 'done': done, 'total': total,
                'eta_sec': max(0, (total - done) * per - elapsed)}

    def progress(self, task, preview, jobs):
        now = time.time()
        calibration = task['ai_calibration']
        state = calibration['state']
        if state in ('rendering', 'thinking') and calibration['rounds']:
            record = calibration['rounds'][-1]
            first = calibration.get('first_round', 0)
            done = record['n'] - first
            total = calibration['max_rounds'] - first + 1
            per = self.seconds_per('calibration')
            eta = (total - done) * per
            stage = 'thinking'
            if state == 'thinking':
                started = epoch(record.get('thinking_started_at'))
                if started is not None:
                    eta -= min(per, max(0, now - started))
            else:
                stage = 'queued'
                for job, status in jobs:
                    if job['job_id'] == record['job_id']:
                        render = self.render_progress(job, status, now)
                        stage = 'rendering' if status['state'] == 'done' else render['stage']
                        if status['state'] not in TERMINAL:
                            eta += render['eta_sec']
                        break
            return {'kind': 'calibration', 'stage': stage, 'done': done, 'total': total,
                    'eta_sec': eta}
        active = [(job, status) for job, status in jobs if status['state'] not in TERMINAL
                  and (job['type'] != 'export_preview' or preview['state'] != 'done')]
        if active:
            job, status = next(((j, s) for j, s in active if s['state'] == 'running'), active[0])
            return self.render_progress(job, status, now)
        return {'kind': None, 'stage': None, 'done': 0, 'total': 0, 'eta_sec': None}
