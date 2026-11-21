"""历史白底只读；仅在本地参考库内保存缩图与状态。"""
import hashlib
import io
import os
import random
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from PIL import Image, ImageOps

KEYWORDS = {'摩卡红橡': ['摩卡红橡', '红橡'], '胡桃木': ['胡桃'], '奶油白': ['奶油白'],
            '北欧橡木': ['北欧橡木', '橡木'], '幻影灰': ['幻影灰'],
            '纯黑': ['纯黑', '黑色', '黑橡'], '长虹玻璃': ['长虹']}
EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
ANNOTATION_DIR = 'refs/annotated'
MAX_ANNOTATION_REGIONS = 100


def empty_library():
    return {'materials': {key: {'keywords': words[:], 'images': []} for key, words in KEYWORDS.items()},
            'style': [], 'annotated': []}


def merge_builtin(library, path):
    """把内置材质参考（refs/builtin.json，安装和升级时总会覆盖）补进用户的参考库。
    只补缺少的条目（按 file 判断），用户已有条目的状态和自己加的图都不动；图片文件不存在的内置条目跳过。"""
    from .core import read_json
    if not path.is_file():
        return library
    try:
        builtin = read_json(path)
    except (OSError, ValueError):
        return library
    root = path.parent.parent

    def add_missing(images, entries):
        known = {entry.get('file') for entry in images}
        for entry in entries or []:
            if entry.get('file') and entry['file'] not in known and (root/entry['file']).is_file():
                images.append({**entry, 'builtin': True})
                known.add(entry['file'])

    for key, group in (builtin.get('materials') or {}).items():
        target = library['materials'].setdefault(key, {'keywords': list(group.get('keywords') or [key]), 'images': []})
        for word in group.get('keywords') or []:
            if word not in target['keywords']:
                target['keywords'].append(word)
        add_missing(target['images'], group.get('images'))
    add_missing(library.setdefault('style', []), builtin.get('style'))
    add_missing(library.setdefault('annotated', []), builtin.get('annotated'))
    return library


def material_key(name, library):
    matches = [(len(word), key) for key, entry in library['materials'].items()
               for word in entry['keywords'] if word.casefold() in name.casefold()]
    return max(matches)[1] if matches else None


def annotation_crop(file, index):
    return f'{ANNOTATION_DIR}/crops/{Path(file).stem}-{index + 1}.jpg'


def jpeg_bytes(image):
    stream = io.BytesIO()
    image.save(stream, 'JPEG', quality=92, subsampling=0)
    return stream.getvalue()


def annotation_jpeg(content):
    from .core import StudioError, MAX_REFERENCE_BYTES
    if not content or len(content) > MAX_REFERENCE_BYTES:
        raise StudioError('单张图片必须大于 0 且不超过 50 MB。', 413)
    try:
        with Image.open(io.BytesIO(content)) as raw:
            if raw.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError('仅支持 JPG、PNG、WebP。')
            image = ImageOps.exif_transpose(raw)
            image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
            rgba = image.convert('RGBA')
            rgb = Image.new('RGB', rgba.size, 'white')
            rgb.paste(rgba, mask=rgba.getchannel('A'))
            return jpeg_bytes(rgb)
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise StudioError('不能读取这张图片。') from error


def stage_annotation_crops(original, regions, crops, stage):
    changes = dict.fromkeys(crops)
    with Image.open(original) as image:
        width, height = image.size
        for path, region in zip(crops, regions):
            x, y, w, h = region['box']
            left, top = min(width-1, round(x*width)), min(height-1, round(y*height))
            right, bottom = min(width, round((x+w)*width)), min(height, round((y+h)*height))
            changes[path] = stage/path.name
            image.crop((left, top, max(left+1, right), max(top+1, bottom))).save(
                changes[path], 'JPEG', quality=92, subsampling=0)
    return changes


def commit_annotation(stage, changes, save):
    # 在保存记录前保留旧文件，写入失败时恢复这一批。
    originals, installed = {}, []
    try:
        for path, replacement in changes.items():
            if path.exists():
                previous = stage/f'old-{path.name}'
                os.replace(path, previous)
                originals[path] = previous
            if replacement is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(replacement, path)
                installed.append(path)
        save()
    except Exception:
        for path in installed:
            if path not in originals:
                path.unlink(missing_ok=True)
        for path, previous in originals.items():
            os.replace(previous, path)
        raise


class RefLibrary:
    def __init__(self, root):
        self.root = Path(root)

    def read(self):
        from .core import read_json
        file = self.root/'refs'/'library.json'
        library = read_json(file) if file.is_file() else empty_library()
        library.setdefault('annotated', [])
        merge_builtin(library, self.root/'refs'/'builtin.json')
        return library

    def save(self, library):
        from .core import atomic_json
        atomic_json(self.root/'refs'/'library.json', library)

    def group(self, library, key):
        from .core import StudioError
        if key == 'style':
            return library['style']
        if key not in library['materials']:
            raise StudioError('没有这个材质分组。')
        return library['materials'][key]['images']

    def add(self, key, source, content=None, library=None, refresh=False):
        from .core import StudioError, inside, backup
        library = self.read() if library is None else library
        images = self.group(library, key)
        if content is None:
            path = Path(source)
            if not path.is_absolute() or path.suffix.lower() not in EXTENSIONS:
                raise StudioError('请拖入图片的完整路径。')
            image_source = path
            fingerprint = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024*1024), b''):
                    fingerprint.update(chunk)
            content_digest = fingerprint.digest()
        elif not 0 < len(content) <= 50*1024*1024:
            raise StudioError('单张图片不能超过 50 MB。', 413)
        else:
            image_source = io.BytesIO(content)
            content_digest = hashlib.sha256(content).digest()
        digest = hashlib.sha256(str(source).encode('utf-8')+b'\0'+content_digest).hexdigest()[:20]
        file = f'refs/{key}/{digest}.jpg'
        prior = next((entry for entry in images if entry['file'] == file or entry['source'] == str(source)), None)
        if prior and not refresh:
            return prior
        target = self.root/(prior['file'] if prior else file)
        if not inside(target, self.root/'refs'):
            raise StudioError('参考库路径越界。', 403)
        try:
            with Image.open(image_source) as image:
                image.thumbnail((1600, 1600))
                thumb = ImageOps.exif_transpose(image)
                if 'A' in thumb.getbands() or 'transparency' in thumb.info:
                    rgba = thumb.convert('RGBA')
                    thumb = Image.new('RGB', rgba.size, 'white')
                    thumb.paste(rgba, mask=rgba.getchannel('A'))
                else:
                    thumb = thumb.convert('RGB')
                target.parent.mkdir(parents=True, exist_ok=True)
                # 内容寻址，同名缩图已存在时直接复用。
                if refresh or not target.exists():
                    backup(target)
                    with target.open('wb' if refresh else 'xb') as stream:
                        thumb.save(stream, 'JPEG', quality=90)
        except (OSError, ValueError) as error:
            raise StudioError('不能读取这张图片：'+str(error)) from error
        if prior:
            return prior
        entry = {'file': file, 'source': str(source), 'status': 'candidate'}
        images.append(entry)
        return entry

    def change_status(self, key, file, status):
        from .core import StudioError
        if status not in ('approved', 'rejected', 'candidate'):
            raise StudioError('参考图状态无效。')
        library = self.read()
        for entry in self.group(library, key):
            if entry['file'] == file:
                entry['status'] = status
                self.save(library)
                return library
        raise StudioError('没有找到这张参考图。', 404)

    def change_material(self, name, keywords=None, create=False):
        from .core import StudioError
        name = name.strip() if isinstance(name, str) else ''
        library = self.read()
        materials = library['materials']
        if create:
            reserved = {'style', 'annotated', 'con', 'aux', 'nul', 'prn',
                        *(f'{prefix}{i}' for prefix in ('com', 'lpt') for i in range(1, 10))}
            if (not name or len(name) > 60 or name.endswith('.') or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                    or name.split('.')[0].casefold() in reserved):
                raise StudioError('材质名称无效。')
            if any(name.casefold() == key.casefold() for key in materials):
                raise StudioError('已有同名材质。', 409)
            materials[name] = {'keywords': [name], 'images': []}
        else:
            self.group(library, name)
            if name not in materials:
                raise StudioError('没有这个材质分组。', 404)
            if (not isinstance(keywords, list) or len(keywords) > 30
                    or any(not isinstance(word, str) or len(word.strip()) > 80 for word in keywords)):
                raise StudioError('关键词格式无效。')
            materials[name]['keywords'] = list(dict.fromkeys(word.strip() for word in keywords if word.strip()))
        self.save(library)
        return library

    def annotation_source(self, source):
        from .core import StudioError, MAX_REFERENCE_BYTES
        if not isinstance(source, str):
            raise StudioError('请拖入图片的完整路径。')
        path = Path(source or '')
        if not path.is_absolute() or path.suffix.lower() not in EXTENSIONS:
            raise StudioError('请拖入图片的完整路径。')
        with path.open('rb') as stream:
            content = stream.read(MAX_REFERENCE_BYTES + 1)
        if not 0 < len(content) <= MAX_REFERENCE_BYTES:
            raise StudioError('单张图片必须大于 0 且不超过 50 MB。', 413)
        return content

    def annotation_path(self, file):
        from .core import StudioError, inside
        target = self.root/file
        root = self.root/ANNOTATION_DIR
        if (not inside(root, self.root) or not inside(root, self.root/'refs') or not inside(target, root)
                or any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)()
                       for p in (self.root/'refs', root, target, target.parent))):
            raise StudioError('标注图路径越界。', 403)
        return target

    def annotation_entry(self, library, file):
        from .core import StudioError
        if isinstance(file, str) and re.fullmatch(r'refs/annotated/[a-f0-9]{20}\.jpg', file):
            for entry in library['annotated']:
                if entry['file'] == file:
                    return entry
        raise StudioError('没有找到这张标注图。', 404)

    def validate_regions(self, regions, materials):
        from .core import StudioError
        if not isinstance(regions, list) or len(regions) > MAX_ANNOTATION_REGIONS:
            raise StudioError(f'一张图最多标注 {MAX_ANNOTATION_REGIONS} 个框。')
        result = []
        for region in regions:
            if not isinstance(region, dict) or not isinstance(region.get('material'), str) or (
                    materials is not None and region['material'] not in materials):
                raise StudioError('请选择现有的材质。')
            box = region.get('box')
            if (not isinstance(box, list) or len(box) != 4
                    or any(type(value) not in (int, float) or not 0 <= value <= 1 for value in box)):
                raise StudioError('标注框坐标无效。')
            x, y, w, h = box
            if not w or not h or x>=1 or y>=1 or x+w > 1+1e-9 or y+h > 1+1e-9:
                raise StudioError('标注框必须在图片范围内。')
            result.append({'material': region['material'], 'box': [x, y, min(w, 1-x), min(h, 1-y)]})
        return result

    def save_annotation(self, regions, *, file=None, source=None, content=None, update_copy=False):
        from .core import StudioError, now, MAX_REFERENCE_BYTES
        library = self.read()
        regions = self.validate_regions(regions, library['materials'])
        original = None
        if file is None:
            if not isinstance(source, str) or not source.strip() or len(source) > 4000:
                raise StudioError('图片来源无效。')
            if not content or len(content) > MAX_REFERENCE_BYTES:
                raise StudioError('单张图片必须大于 0 且不超过 50 MB。', 413)
            digest = hashlib.sha256(source.encode('utf-8') + b'\0' + hashlib.sha256(content).digest()).hexdigest()[:20]
            file = f'{ANNOTATION_DIR}/{digest}.jpg'
            entry = next((e for e in library['annotated'] if e['file'] == file), None)
            if entry is None:
                original = content if update_copy else annotation_jpeg(content)
                entry = {'file': file, 'source': source, 'created_at': now(), 'regions': []}
                library['annotated'].append(entry)
            elif not update_copy:
                raise StudioError('这张图已经加入参考库，请打开已有标注编辑。', 409)
        else:
            entry = self.annotation_entry(library, file)
        target = self.annotation_path(file)
        crops = [self.annotation_path(annotation_crop(file, i)) for i in range(max(len(regions), len(entry['regions'])))]
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='.annotation-', dir=target.parent) as temporary:
            stage = Path(temporary)
            staged_original = target
            if original is not None:
                staged_original = stage/'original.jpg'
                staged_original.write_bytes(original)
            # 新增和编辑都从同一个 JPEG 版本裁图。
            changes = stage_annotation_crops(staged_original, regions, crops, stage)
            if original is not None:
                changes[target] = staged_original
            entry['regions'] = regions
            commit_annotation(stage, changes, lambda: self.save(library))
        return entry

    def delete_annotation(self, file):
        library = self.read()
        entry = self.annotation_entry(library, file)
        paths = [self.annotation_path(annotation_crop(file, i)) for i in range(len(entry['regions']))]
        paths.append(self.annotation_path(file))
        library['annotated'].remove(entry)
        paths[-1].parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='.annotation-', dir=paths[-1].parent) as stage:
            commit_annotation(Path(stage), dict.fromkeys(paths), lambda: self.save(library))


def history_images(roots):
    """不跟随链接；仅白底目录内的图片，排除遮罩、TD 和通道。"""
    seen = set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for current, directories, names in os.walk(root, followlinks=False):
            folder = Path(current)
            directories[:] = sorted(n for n in directories if not (folder/n).is_symlink()
                                     and not getattr(folder/n, 'is_junction', lambda: False)())
            if not any(p.casefold() in ('sku', '白底') for p in folder.relative_to(root).parts) and folder.name.casefold() not in ('sku', '白底'):
                continue
            if any(p in ('旧版', '_extra') for p in folder.relative_to(root).parts):
                continue
            for name in sorted(names):
                path = folder/name
                if path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                    continue
                if re.search(r'遮罩|通道|(?:^|[^a-z])td(?:[^a-z]|$)', path.stem, re.I):
                    continue
                normalized = str(path.resolve()).casefold()
                if normalized not in seen:
                    seen.add(normalized)
                    yield path


def build_library(root, history_roots):
    library_store = RefLibrary(root)
    library = library_store.read()
    all_images = list(history_images(history_roots))
    errors = []
    for path in all_images:
        keys = [key for key, entry in library['materials'].items() if any(word in path.stem for word in entry['keywords'])]
        for key in keys:
            try:
                library_store.add(key, str(path), library=library, refresh=True)
            except Exception as error:
                errors.append({'source': str(path), 'error': str(error)})
    for path in random.Random(20261120).sample(all_images, min(12, len(all_images))):
        try:
            library_store.add('style', str(path), library=library, refresh=True)
        except Exception as error:
            errors.append({'source': str(path), 'error': str(error)})
    library_store.save(library)
    return {'materials': {key: len(entry['images']) for key, entry in library['materials'].items()},
            'style': len(library['style']), 'scanned_images': len(all_images), 'errors': errors,
            'missing_roots': [str(p) for p in history_roots if not Path(p).is_dir()]}
