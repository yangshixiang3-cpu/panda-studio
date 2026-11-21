"""桌面输入的纯路径判断；文件类型由调用方提供，不读取文件系统。"""
from pathlib import PurePosixPath, PureWindowsPath

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
MODEL_EXTENSIONS = {'.max'}  # 只导入 Max 文件，不再接受 FBX
PRODUCT_SUBFOLDERS = {'模型', '参考图', 'sku', '视角图'}


def resolve_input_path(path, is_directory=False):
    if not isinstance(path, str) or not path.strip():
        return {'kind': 'missing'}
    value = PureWindowsPath(path) if '\\' in path or (len(path) > 1 and path[1] == ':') else PurePosixPath(path)
    if not value.is_absolute():
        return {'kind': 'missing'}
    if is_directory:
        product = value.parent if value.name.casefold() in PRODUCT_SUBFOLDERS else value
        return {'kind': 'product', 'product_dir': str(product), 'selected_model': ''}
    if value.suffix.lower() in MODEL_EXTENSIONS:
        product = next((p.parent for p in value.parents if p.name == '模型'), value.parent)
        return {'kind': 'model', 'product_dir': str(product), 'selected_model': str(value)}
    if value.suffix.lower() in IMAGE_EXTENSIONS:
        return {'kind': 'image', 'path': str(value)}
    return {'kind': 'ignored'}


def plan_inputs(entries):
    """保留第一个产品/模型和所有图片，其余计入提示；不依赖输入的存在性。"""
    result = {'product': None, 'images': [], 'ignored': 0, 'missing': 0}
    for entry in entries:
        resolved = resolve_input_path(entry.get('path'), entry.get('is_directory', False))
        kind = resolved['kind']
        if kind in ('product', 'model') and result['product'] is None:
            result['product'] = resolved
        elif kind == 'image':
            result['images'].append(resolved['path'])
        elif kind == 'missing':
            result['missing'] += 1
        else:
            result['ignored'] += 1
    return result
