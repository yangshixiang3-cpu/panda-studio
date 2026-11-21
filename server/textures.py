"""在任务的产品目录内按文件名查找预览贴图。"""
from collections import deque
from pathlib import Path
from threading import Lock

from .core import StudioError, inside


IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
SKIP_FOLDERS = {
    'sku', '旧版', '视角图', '参考图', '光向参考', 'exchange', 'autoback',
    'final', 'smallshots', 'preview', '_extra',
}


def texture_entries(folder):
    try:
        return sorted(folder.iterdir(), key=lambda entry: (entry.name.casefold(), entry.name))
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return ()


def ordinary_folder(path):
    return not path.is_symlink() and not path.is_junction() and path.is_dir()


def matching_file(entries, name, product):
    for entry in entries:
        if (entry.name.casefold() == name and not entry.is_symlink()
                and inside(entry, product) and entry.is_file()):
            return entry
    return None


def search_texture(product, model_folder, name):
    if inside(model_folder, product):
        entries = texture_entries(model_folder)
        match = matching_file(entries, name, product)
        if match:
            return match
        for preferred in ('maps', '贴图'):
            for folder in entries:
                if folder.name.casefold() == preferred and ordinary_folder(folder):
                    match = matching_file(texture_entries(folder), name, product)
                    if match:
                        return match

    queue = deque([(product, 0)])
    while queue:
        folder, depth = queue.popleft()
        entries = texture_entries(folder)
        match = matching_file(entries, name, product)
        if match:
            return match
        if depth < 5:
            queue.extend((entry, depth + 1) for entry in entries
                         if entry.name.casefold() not in SKIP_FOLDERS and ordinary_folder(entry))
    return None


class TaskTextures:
    def __init__(self, studio):
        self.studio = studio
        self.results = {}
        self.lock = Lock()

    def find(self, task_id, name):
        if (not name or '..' in name or any(char in name for char in '/\\:<>|"?*')
                or any(ord(char) < 32 for char in name)):
            raise StudioError('贴图名称必须是纯文件名。')
        if Path(name).suffix.casefold() not in IMAGE_EXTENSIONS:
            raise StudioError('不支持这个贴图文件扩展名。')

        task = self.studio.load(task_id)
        product_path = Path(task['product_dir'])
        model_path = Path(task.get('model_max') or '')
        if not product_path.is_absolute():
            raise StudioError('任务的产品路径无效。')
        product = product_path.resolve()
        model_folder = model_path.parent.resolve() if model_path.is_absolute() else product
        key = task_id, product, model_folder, name.casefold()
        with self.lock:
            if key not in self.results:
                self.results[key] = search_texture(product, model_folder, key[-1])
            match = self.results[key]
        if match is not None and not match.is_symlink() and inside(match, product) and match.is_file():
            return match
        raise StudioError('没有找到贴图：' + name, 404)
