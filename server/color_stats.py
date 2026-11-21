"""分区颜色统计；只读输入，不写文件，不依赖渲染器。"""
import math
from PIL import Image


def srgb_to_lab(rgb):
    values = [v / 255 for v in rgb]
    r, g, b = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
    xyz = ((.4124564*r + .3575761*g + .1804375*b)/.95047,
           .2126729*r + .7151522*g + .0721750*b,
           (.0193339*r + .1191920*g + .9503041*b)/1.08883)
    x, y, z = [v ** (1/3) if v > (6/29)**3 else v/(3*(6/29)**2)+4/29 for v in xyz]
    return [116*y-16, 500*(x-y), 200*(y-z)]


def color_statistics(image, regions, legend, tolerance=30):
    """匹配原色及 gamma 编码色的最近欧氏距离；平均 Lab 使用逐像素 D65 Lab。"""
    image, regions = image.convert('RGB'), regions.convert('RGB')
    if image.size != regions.size:
        raise ValueError('小样和材质分区图尺寸不一致。')
    colors = []
    accum = {}
    for hex_color, name in legend.items():
        if len(hex_color) != 7 or not hex_color.startswith('#'):
            raise ValueError('材质分区图例的颜色无效。')
        rgb = tuple(int(hex_color[i:i+2], 16) for i in (1, 3, 5))
        colors.append((rgb, name))
        colors.append((tuple(255*(c/255)**(1/2.2) for c in rgb), name))
        accum.setdefault(name, [0, [0., 0., 0.], [0., 0., 0.], 0., 0., 0])
    classify, lab_cache = {}, {}
    pixels = lambda img: img.get_flattened_data() if hasattr(img, 'get_flattened_data') else img.getdata()
    for rgb, region in zip(pixels(image), pixels(regions)):
        if region not in classify:
            nearest = min(((sum((a-b)**2 for a, b in zip(region, color)), name)
                           for color, name in colors), key=lambda candidate: candidate[0], default=None)
            classify[region] = (nearest[1] if region != (0, 0, 0) and nearest
                                and nearest[0] <= tolerance**2 else None)
        name = classify[region]
        if name is None:
            continue
        if rgb not in lab_cache:
            lab_cache[rgb] = srgb_to_lab(rgb)
        lab = lab_cache[rgb]
        a = accum[name]
        a[0] += 1
        for i in range(3):
            a[1][i] += rgb[i]
            a[2][i] += lab[i]
        brightness = .2126*rgb[0] + .7152*rgb[1] + .0722*rgb[2]
        a[3] += brightness
        a[4] += brightness**2
        a[5] += brightness > 245
    total = image.width * image.height
    result = {}
    for name, (count, rgb, lab, bright, squared, highlights) in accum.items():
        result[name] = {'pixels': count, 'area_ratio': count/total if total else 0,
                        'mean_srgb': [v/count for v in rgb] if count else None,
                        'mean_lab': [v/count for v in lab] if count else None,
                        'brightness_std': math.sqrt(max(0, squared/count-(bright/count)**2)) if count else 0,
                        'highlight_ratio': highlights/count if count else 0}
    return result


def image_statistics(image_path, regions_path, legend):
    with Image.open(image_path) as image, Image.open(regions_path) as regions:
        return color_statistics(image, regions, legend)
