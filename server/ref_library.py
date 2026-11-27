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


RESERVED_NAMES = {'style', 'annotated', 'con', 'aux', 'nul', 'prn',
                  *(f'{prefix}{i}' for prefix in ('com', 'lpt') for i in range(1, 10))}
BAD_NAME_CHARS = r'[<>:"/\\|?*\x00-\x1f]'

_MODEL_PREFIX_RE = re.compile(
    r'^(?:(?:mtl|mat|vray|corona|cr|std|pbr|tex|id|sub)(?:[\s_\-\.:#/]+\d*|\d+)|m\d*[\s_\-\.:#/]+|\d+[\s_\-\.:#/]+)+',
    re.I,
)
_GENERIC_MAX_NAME_RE = re.compile(
    r'^(?:material|default|standard|corona\w*|vray\w*|physical\w*)[\s_\-\.#0-9]*$',
    re.I,
)
_BRACKET_SUFFIX_RE = re.compile(r'(?:\s*[\(\[（【][^)\]）】]{0,20}[\)\]）】])+$')
_TRAIL_SUFFIX_RE = re.compile(
    r'(?:[\s_\-\.#/]+\d+|(?<=[^\da-zA-Z\s_\-\.#/])\d+|[\s_\-\./]+(?:copy|inst|instance|v\d+|[a-z]))+$',
    re.I,
)
_SEP_STRIP_RE = re.compile(r'^[\s_\-\./:：·|#]+|[\s_\-\./:：·|#]+$')
_PART_PREFIXES = (
    '柜体', '柜门', '门板', '侧板', '背板', '层板', '顶板', '底板', '面板', '台面',
    '抽屉', '抽面', '框架', '脚架', '柜脚', '桌腿', '拉手', '把手', '封边', '格栅',
    '线条', '内衬', '主体', '外框', '边框', '主材', '辅材', '材质',
)
_FINISH_SUFFIXES = (
    '木饰面', '饰面板', '饰面', '木皮', '贴皮', '贴面', '清漆', '木纹', '板材', '材质',
)
GENERIC_MATERIAL_WORDS = frozenset({
    '玻璃', '清玻', '茶玻', '灰玻', '黑玻', '白玻', '超白玻', '超白玻璃', '钢化玻璃',
    '普通玻璃', '透明玻璃', '磨砂玻璃', '有色玻璃', '茶色玻璃', '灰色玻璃', '黑色玻璃',
    '木纹', '木皮', '实木', '板材', '饰面', '烤漆', '清漆', '油漆', '肤感', '哑光', '亮光', '拉丝',
    '金属', '五金', '不锈钢', '铝合金', '铁艺', '石材', '大理石', '岩板', '皮革', '布艺', '塑料',
    '深色', '浅色', '亮色', '暗色', '纯色', '透明',
    '材质', '默认', '其他', 'default', 'material', 'standard', 'physical', 'corona', 'vray', 'metal', 'glass', 'wood',
})
SYNONYM_RULES = (
    ('摩卡红橡', ('摩卡红橡', '北美红橡', '红橡木', '摩卡橡木', '深红橡', '红橡', 'red oak')),
    ('胡桃木', ('北美黑胡桃', '焦糖胡桃', '黑胡桃', '北美胡桃', '深胡桃', '浅胡桃', '黄金胡桃', '秋木胡桃', '胡桃木色', '胡桃色', 'walnut')),
    ('奶油白', ('奶油肤感', '奶白色', '暖白色', '米白色', '羊绒白', '象牙白', '珍珠白', '肤感白', '柔光白', '奶油色', '暖白', '奶白', '米白')),
    ('北欧橡木', ('北欧白橡', '自然橡木', '日系原木', '白蜡木', '白橡木', '原木色', '浅橡木', '水曲柳', '原木', '白蜡', '白橡', '浅橡', 'ash wood', 'white oak')),
    ('幻影灰', ('莫兰迪灰', '太空灰', '岩石灰', '水泥灰', '绅士灰', '石墨灰', '深灰色', '高级灰', '深灰', '暗灰', '烟灰', '铁灰', '暖灰', '冷灰', '中灰')),
    ('纯黑', ('曜石黑', '哑光黑', '拉丝黑', '磨砂黑', '烤漆黑', '哑黑', '雅黑', '碳黑', '炭黑', '亮黑', '黑砂', '黑钛', '深黑')),
    ('长虹玻璃', ('长虹玻璃', '长虹玻', '长虹')),
)


def valid_material_name(name):
    return bool(name) and len(name) <= 60 and not name.endswith('.') and not re.search(BAD_NAME_CHARS, name) \
        and name.split('.')[0].casefold() not in RESERVED_NAMES


def clean_material_name(name):
    """场景材质名、新材质名 → 能当参考库分组的名字（去掉文件名里不能用的字符）；整理后还不能用就返回空。"""
    name = re.sub(r'\s+', ' ', re.sub(BAD_NAME_CHARS, ' ', str(name or ''))).strip().rstrip('.').strip()[:60].strip()
    return name if valid_material_name(name) else ''


def core_material_name(name):
    """剥除 Max 常见建模前后缀、部件前缀、工艺后缀与尾号，提取核心材质词。"""
    text = str(name or '').strip()
    if not text or _GENERIC_MAX_NAME_RE.fullmatch(text):
        return ''
    text = _BRACKET_SUFFIX_RE.sub('', text).strip()
    text = _MODEL_PREFIX_RE.sub('', text).strip()
    text = _TRAIL_SUFFIX_RE.sub('', text).strip()
    text = _SEP_STRIP_RE.sub('', text)
    if not text or _GENERIC_MAX_NAME_RE.fullmatch(text):
        return ''
    changed = True
    while changed and text:
        changed = False
        for prefix in _PART_PREFIXES:
            if text.startswith(prefix):
                rest = _SEP_STRIP_RE.sub('', text[len(prefix):])
                if len(rest) >= 2:
                    text = rest
                    changed = True
                    break
    changed = True
    while changed and text:
        changed = False
        for suffix in _FINISH_SUFFIXES:
            if text.endswith(suffix):
                rest = _SEP_STRIP_RE.sub('', text[:-len(suffix)])
                if len(rest) >= 2:
                    text = rest
                    changed = True
                    break
    return _SEP_STRIP_RE.sub('', text)


def _allow_fluted_glass(raw_folded, key, word=''):
    """普通玻璃/清玻/茶玻等绝不能误配到长虹玻璃；只有含「长虹」才允许配长虹玻璃。"""
    if '长虹' in key.casefold() or '长虹' in word.casefold():
        return '长虹' in raw_folded
    return True


def material_key(name, library):
    if not isinstance(name, str) or not name.strip() or not isinstance(library, dict):
        return None
    materials = library.get('materials') or {}
    if not materials:
        return None
    raw = name.strip()
    raw_folded = raw.casefold()
    core = core_material_name(raw)
    core_folded = core.casefold()

    # 0) 原名与分组名精确匹配最高优先级
    for key in materials:
        if key.casefold() == raw_folded and _allow_fluted_glass(raw_folded, key):
            return key

    # ① 清洗前后与关键词精确相等（最长词优先）
    exact_matches = [
        (len(word), key)
        for key, entry in materials.items()
        for word in (entry.get('keywords') or [key])
        if word and _allow_fluted_glass(raw_folded, key, word)
        and word.casefold() in (raw_folded, core_folded)
    ]
    if exact_matches:
        return max(exact_matches)[1]

    # ② 关键词包含于场景材质名（最长词优先）
    sub_matches = [
        (len(word), key)
        for key, entry in materials.items()
        for word in (entry.get('keywords') or [key])
        if word and _allow_fluted_glass(raw_folded, key, word)
        and (word.casefold() in raw_folded or (core_folded and word.casefold() in core_folded))
    ]
    best_sub = max(sub_matches) if sub_matches else None

    # ③ 清洗后的核心材质名（长度 ≥ 2 且非泛化材质词）包含于参考库组名/关键词
    best_reverse = None
    if len(core_folded) >= 2 and core_folded not in GENERIC_MATERIAL_WORDS:
        reverse_matches = []
        for key, entry in materials.items():
            configured = entry.get('keywords')
            default_words = KEYWORDS.get(key)
            if not configured or configured == default_words:
                targets = list(dict.fromkeys([*(configured or []), key]))
            else:
                targets = list(configured)
            for target in targets:
                tf = target.casefold()
                if target and _allow_fluted_glass(raw_folded, key, target) and core_folded in tf:
                    reverse_matches.append((core_folded == tf, len(core_folded), -len(tf), key))
        if reverse_matches:
            best_reverse = max(reverse_matches)

    if best_sub and best_reverse:
        return best_reverse[3] if best_reverse[1] > best_sub[0] else best_sub[1]
    if best_sub:
        return best_sub[1]
    if best_reverse:
        return best_reverse[3]

    # ④ 安全同义词兜底映射（普通玻璃绝不误配长虹玻璃）
    synonym_matches = []
    for target_key, aliases in SYNONYM_RULES:
        if not _allow_fluted_glass(raw_folded, target_key):
            continue
        resolved = target_key if target_key in materials else next(
            (k for k in materials if k.casefold() == target_key.casefold()), None
        )
        if resolved is None:
            continue
        for alias in aliases:
            af = alias.casefold()
            if not _allow_fluted_glass(raw_folded, resolved, alias):
                continue
            if af in (raw_folded, core_folded):
                synonym_matches.append((2, len(af), resolved))
            elif af in raw_folded or (core_folded and af in core_folded):
                synonym_matches.append((1, len(af), resolved))
    if synonym_matches:
        return max(synonym_matches)[2]
    return None


def effective_material_ref(name, library, bindings=None):
    """结合显式 material_bindings 与自动 material_key 返回实际生效的参考库材质键。"""
    if not isinstance(name, str) or not name.strip():
        return None
    materials = (library or {}).get('materials') or {}
    if isinstance(bindings, dict) and name in bindings:
        bound = bindings[name]
        if bound == '__none__':
            return None
        if isinstance(bound, str) and bound:
            if bound in materials:
                return bound
            matched = next((k for k in materials if k.casefold() == bound.casefold()), None)
            if matched is not None:
                return matched
    return material_key(name, library)


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
            if not valid_material_name(name):
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

    def library_material(self, library, name, source=None):
        """AI 校准里框的材质 → 参考库里的材质分组：参考库材质直接用；场景材质、新材质先找同名，
        再按关键词归（如「柜门 胡桃饰面」→ 胡桃木），都对不上就新建一个同名分组（关键词就是这个名字）。"""
        materials = library['materials']
        if not isinstance(name, str) or not name.strip():
            return None
        if source == 'library' and name in materials:
            return name
        folded = name.strip().casefold()
        same = next((key for key in materials if key.casefold() == folded), None)
        key = same or material_key(name, library)
        if key:
            return key
        clean = clean_material_name(name)
        if not clean:
            return None
        same = next((key for key in materials if key.casefold() == clean.casefold()), None)
        if same:
            return same
        materials[clean] = {'keywords': [clean], 'images': []}
        return clean

    def _sha(self, entry):
        if not entry.get('sha'):
            try:
                entry['sha'] = hashlib.sha256(self.annotation_path(entry['file']).read_bytes()).hexdigest()
            except (OSError, ValueError, KeyError):
                return None
        return entry['sha']

    def absorb(self, original, regions, source, linked=None):
        """把一张 AI 校准的材质参考收进参考库的标注参考图，每个框归到对应材质，参考库页对应材质下就能看到裁图。
        - original：任务里的参考图（已经处理成 JPEG）；regions：任务里的框（带 material_source）；
        - linked：这张图是从参考库哪张标注图点选来的，有就并进那一张；
        - 同一张图（内容一样）在别的任务里又用了：并进已有的那一张，不重复加。合并时这张图里出现的材质，
          框换成这次的；别的材质的框保留。
        没有框（整体参考）不收。返回参考库里的文件名。"""
        from .core import now
        library = self.read()
        mapped = []
        for region in regions or []:
            key = self.library_material(library, region.get('material'), region.get('material_source'))
            if key:
                mapped.append({'material': key, 'box': region['box']})
        if not mapped:
            return None
        content = Path(original).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        entry = next((e for e in library['annotated'] if linked and e['file'] == linked), None)
        entry = entry or next((e for e in library['annotated'] if self._sha(e) == digest), None)
        new = entry is None
        if new:
            entry = {'file': f'{ANNOTATION_DIR}/{digest[:20]}.jpg', 'source': source, 'created_at': now(),
                     'regions': [], 'sha': digest}
            library['annotated'].append(entry)
        touched = {region['material'] for region in mapped}
        regions = self.validate_regions([r for r in entry['regions'] if r['material'] not in touched] + mapped,
                                        library['materials'])
        file = entry['file']
        target = self.annotation_path(file)
        crops = [self.annotation_path(annotation_crop(file, i)) for i in range(max(len(regions), len(entry['regions'])))]
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='.annotation-', dir=target.parent) as temporary:
            stage = Path(temporary)
            staged_original = target
            if new:
                staged_original = stage/'original.jpg'
                staged_original.write_bytes(content)
            changes = stage_annotation_crops(staged_original, regions, crops, stage)
            if new:
                changes[target] = staged_original
            entry['regions'] = regions
            commit_annotation(stage, changes, lambda: self.save(library))
        return file

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
