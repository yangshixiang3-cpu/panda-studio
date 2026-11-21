"""白底工作台的纯 Python 计算、队列和文件协议；不依赖 Max。"""

from __future__ import annotations

import json
import colorsys
import math
import os
import re
import shutil
from stat import S_ISREG
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


LOG_LIMIT = 200
RUNNER_LOG_BYTES = 1024 * 1024
_log_lock = threading.Lock()
TERMINAL_STATES = frozenset(("done", "failed"))
JOB_STATES = frozenset(("waiting_lock", "running", "done", "failed"))
ITEM_STATES = frozenset(("pending", "rendering", "done", "failed"))
LIGHTING_DEFAULT_KEY_INTENSITY = 1


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def backup_existing(path, *, move=False) -> Optional[Path]:
    """保留最近一次的输出。"""
    path = Path(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("目标不是普通文件：{}".format(path))
    backup = path.with_name(path.name + ".prev")
    if move:
        os.replace(path, backup)
    else:
        shutil.copy2(path, backup)
    return backup


def atomic_write_text(path, text):
    """同目录 .tmp + fsync + os.replace；失败时保留旧目标及临时文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(30):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(.02)


def atomic_write_json(path, document):
    # 在碰文件前拒绝 NaN、Infinity 等非法协议值。
    text = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
    atomic_write_text(path, text + "\n")


def log_lines(message):
    return str(message).splitlines() or [""]


def append_runner_log(exchange_dir, message):
    path = Path(exchange_dir) / "runner.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = "".join(f"{timestamp()} {line}\n" for line in log_lines(message)).encode("utf-8")
    with _log_lock:
        if path.is_file() and path.stat().st_size + len(entry) > RUNNER_LOG_BYTES:
            for number in (3, 2, 1):
                previous = path.with_name(f"runner.log.{number - 1}") if number > 1 else path
                if previous.is_file():
                    os.replace(previous, path.with_name(f"runner.log.{number}"))
        with path.open("ab") as stream:
            stream.write(entry)


class RunnerHeartbeat:
    """后台线程只写磁盘，使同步渲染期间的心跳也能续期。"""
    def __init__(self, exchange_dir, version, on_error, interval=3):
        self.path = Path(exchange_dir) / "runner.heartbeat.json"
        self.version = version
        self.on_error = on_error
        self.interval = interval
        self.job_id = None
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="白底执行器心跳")

    def _write(self):
        document = {"ts": timestamp(), "epoch": time.time(), "pid": os.getpid(),
                    "busy": self.job_id is not None, "job_id": self.job_id,
                    "runner_version": self.version}
        if self.stopping.is_set():
            document["stopped"] = True
        try:
            atomic_write_json(self.path, document)
        except OSError as error:
            self.on_error(f"无法写入执行器心跳：{error}")

    def _run(self):
        while not self.stopping.wait(self.interval):
            self.beat()

    def start(self):
        self.beat()
        self.thread.start()

    def beat(self):
        with self.lock:
            if not self.stopping.is_set():
                self._write()

    def set_job(self, job_id):
        with self.lock:
            self.job_id = job_id
            if not self.stopping.is_set():
                self._write()

    def stop(self):
        with self.lock:
            if not self.stopping.is_set():
                self.stopping.set()
                self._write()


def resolve_path(value, base):
    path = Path(value).expanduser()
    return str(path.resolve() if path.is_absolute() else (Path(base) / path).resolve())


def load_config(path):
    path = Path(path).expanduser().resolve()
    config = read_json(path)
    if not isinstance(config, dict):
        raise ValueError("config.json 必须是对象")
    config = dict(config)
    base = path.parent
    config["exchange_dir"] = resolve_path(config.get("exchange_dir", "exchange"), base)
    for key in ("template_max", "render_preset_rps"):
        if config.get(key):
            config[key] = resolve_path(config[key], base)
    if "template_anchor_mm" in config or "template_anchor" in config:
        config["template_anchor"] = template_anchor_from(config)
    config.setdefault("render_preset_rps", None)
    config.setdefault("smallshot_time_limit_sec", 25)
    config.setdefault("max_lock_owner_prefix", "WhiteBG")
    config.setdefault("key_light_intensity", LIGHTING_DEFAULT_KEY_INTENSITY)
    return config


@dataclass(frozen=True)
class QueuedJob:
    path: Path
    data: dict

    @property
    def status_path(self):
        return self.path.with_name(self.path.stem + ".status.json")


def created_at_key(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("created_at 必须带时区")
    return parsed.astimezone(timezone.utc)


def read_status(path):
    if not Path(path).exists():
        return None
    status = read_json(path)
    if not isinstance(status, dict) or status.get("state") not in JOB_STATES:
        raise ValueError("状态文件格式错误：{}".format(path))
    return status


def pending_jobs(jobs_dir, on_error: Optional[Callable] = None):
    """按绝对时间、文件名稳定排序；损坏文件交给回调，绝不当作新任务重跑。"""
    pending = []
    for path in sorted(Path(jobs_dir).glob("*.json")):
        if path.name.endswith(".status.json"):
            continue
        try:
            status = read_status(path.with_name(path.stem + ".status.json"))
            if status and status["state"] in TERMINAL_STATES:
                continue
            data = read_json(path)
            if not isinstance(data, dict) or data.get("job_id") != path.stem:
                raise ValueError("任务 job_id 必须与文件名相同")
            if "template_anchor_mm" in data:
                raise ValueError("任务使用已废弃的锚点键，请改为 template_anchor 对象")
            sort_key = created_at_key(data["created_at"])
            pending.append((sort_key, path.name, QueuedJob(path, data)))
        except Exception as exc:
            if on_error is None:
                raise
            on_error(path, exc)
    return [entry[2] for entry in sorted(pending, key=lambda entry: entry[:2])]


def next_job(jobs_dir, on_error=None):
    jobs = pending_jobs(jobs_dir, on_error)
    return jobs[0] if jobs else None


def new_status(job):
    now = timestamp()
    return {
        "job_id": job["job_id"], "state": "waiting_lock", "started_at": None,
        "updated_at": now,
        "items": [
            {"no": item["no"], "state": "pending", "out_image": item["out_image"],
             "out_mask": item.get("out_mask"), "message": ""}
            for item in job.get("items", [])
        ],
        "message": "", "log": [],
    }


def add_status_log(status, message):
    lines = []
    for existing in status.get("log", []):
        lines.extend(log_lines(existing))
    lines.extend("{} {}".format(timestamp(), line) for line in log_lines(message))
    status["log"] = lines[-LOG_LIMIT:]


def write_status(path, status):
    if status.get("state") not in JOB_STATES:
        raise ValueError("未知任务状态")
    if any(item.get("state") not in ITEM_STATES for item in status.get("items", [])):
        raise ValueError("未知明细状态")
    status["updated_at"] = timestamp()
    status["log"] = [line for entry in status.get("log", []) for line in log_lines(entry)][-LOG_LIMIT:]
    atomic_write_json(path, status)


def vector3(value):
    if len(value) != 3:
        raise ValueError("坐标必须有三个分量")
    result = tuple(float(component) for component in value)
    if not all(math.isfinite(component) for component in result):
        raise ValueError("坐标必须为有限数值")
    return result


def unit_scale(mm_per_unit):
    value = float(mm_per_unit)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("每系统单位毫米数必须大于零且为有限数值")
    return value


def mm_to_units(value, mm_per_unit):
    return float(value) / unit_scale(mm_per_unit)


def units_to_mm(value, mm_per_unit):
    return float(value) * unit_scale(mm_per_unit)


def pf1_to_max(point_mm, origin_max=(0, 0, 0), mm_per_unit=1.0):
    """PF1 毫米点 → Max 系统单位点；origin_max 是产品当前世界包围盒中心。"""
    x, y, z = vector3(point_mm)
    origin = vector3(origin_max)
    scale = unit_scale(mm_per_unit)
    offset = (x / scale, -z / scale, y / scale)
    return tuple(a + b for a, b in zip(origin, offset))


def max_to_pf1(point_max, origin_max=(0, 0, 0), mm_per_unit=1.0):
    point, origin = vector3(point_max), vector3(origin_max)
    scale = unit_scale(mm_per_unit)
    x, y, z = ((a - b) * scale for a, b in zip(point, origin))
    return x, z, -y


def horizontal_fov_deg(fov_y_deg, width, height):
    angle = float(fov_y_deg)
    width, height = float(width), float(height)
    if not (math.isfinite(angle) and 0 < angle < 180):
        raise ValueError("竖直视场角必须在 0 与 180 度之间")
    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
        raise ValueError("输出宽高必须大于零且为有限数值")
    return math.degrees(2 * math.atan(math.tan(math.radians(angle) / 2) * width / height))


VERTICAL_CORRECTION_MAX_DEG = 45


def camera_vertical_correction(camera, width, height):
    """由 PF1 相机计算水平目标和画面相对水平光轴的竖直平移。"""
    if camera.get("frame") != "PF1":
        raise ValueError("只接受 PF1 相机")
    if camera.get("vertical_correction") is not True:
        return None
    position, target = vector3(camera["position_mm"]), vector3(camera["target_mm"])
    depth = math.hypot(target[0] - position[0], target[2] - position[2])
    dy = target[1] - position[1]
    elevation = math.degrees(math.atan2(dy, depth))
    if depth == 0 or abs(elevation) > VERTICAL_CORRECTION_MAX_DEG + 1e-10:
        return None
    hfov = horizontal_fov_deg(camera["fov_y_deg"], width, height)
    center_slope = dy / depth
    # 渲染内容的移动与取景窗口的移动方向相反。Max 按全胶片宽度计。
    return {
        "target_mm": (target[0], position[1], target[2]),
        "horizontal_fov_deg": hfov,
        "image_shift_film_width": -center_slope / (2 * math.tan(math.radians(hfov) / 2)),
    }


class VariantNodesNotFound(ValueError):
    def __init__(self):
        super().__init__("场景里找不到这个柜子的物体，模型可能改过，请在网页里重新识别柜子")


def variant_node_selection(geometry_names, parents, auxiliaries, keep_nodes):
    """按原始 Max 名称筛选几何体，并找出失去所有几何成员的组头及辅助父节点。"""
    names = set(keep_nodes)
    kept = {node for node, name in geometry_names.items() if name in names}
    if not kept:
        raise VariantNodesNotFound()

    def ancestors(nodes):
        result = set()
        for node in nodes:
            parent = parents.get(node)
            while parent in parents and parent not in result:
                result.add(parent)
                parent = parents[parent]
        return result

    removed = geometry_names.keys() - kept
    empty_auxiliaries = set(auxiliaries) & (ancestors(removed) - ancestors(kept))
    return kept, removed | empty_auxiliaries


def validate_bbox(minimum, maximum):
    low, high = vector3(minimum), vector3(maximum)
    if any(a > b for a, b in zip(low, high)):
        raise ValueError("包围盒最小值大于最大值")
    return low, high


def bbox_center(minimum, maximum):
    low, high = validate_bbox(minimum, maximum)
    return tuple((a + b) / 2 for a, b in zip(low, high))


def bottom_center(minimum, maximum):
    low, high = validate_bbox(minimum, maximum)
    return (low[0] + high[0]) / 2, (low[1] + high[1]) / 2, low[2]


def union_bbox(boxes):
    boxes = [validate_bbox(*box) for box in boxes]
    if not boxes:
        raise ValueError("没有可用网格，无法计算包围盒")
    return (tuple(min(box[0][axis] for box in boxes) for axis in range(3)),
            tuple(max(box[1][axis] for box in boxes) for axis in range(3)))


def validate_template_anchor(anchor):
    """贴墙锚点是 Max 世界毫米对象；不接受旧三元组或 null。"""
    keys = ("x_mm", "back_y_mm", "floor_z_mm")
    if not isinstance(anchor, dict) or any(key not in anchor for key in keys):
        raise ValueError("template_anchor 必须包含 x_mm、back_y_mm、floor_z_mm")
    if any(isinstance(anchor[key], bool) for key in keys):
        raise ValueError("贴墙锚点分量必须是有限数值")
    values = vector3([anchor[key] for key in keys])
    return dict(zip(keys, values))


def template_anchor_from(document, fallback=None):
    """任务显式值优先；未提供时使用配置；旧键始终报错，不自动迁移。"""
    if "template_anchor_mm" in document or (fallback is not None and "template_anchor_mm" in fallback):
        raise ValueError("旧锚点键已停用，请改为 template_anchor 对象")
    if "template_anchor" in document:
        return validate_template_anchor(document["template_anchor"])
    if fallback is not None:
        return template_anchor_from(fallback)
    raise ValueError("缺少 template_anchor 贴墙锚点")


def placement_from_bbox(minimum, maximum, template_anchor, mm_per_unit):
    """可见 bbox 用系统单位；x 中心、y 最大面、z 最小面对齐毫米锚点。"""
    low, high = validate_bbox(minimum, maximum)
    template_anchor = validate_template_anchor(template_anchor)
    center = bbox_center(minimum, maximum)
    bottom = bottom_center(minimum, maximum)
    reference = (center[0], high[1], low[2])
    anchor = tuple(mm_to_units(template_anchor[key], mm_per_unit)
                   for key in ("x_mm", "back_y_mm", "floor_z_mm"))
    delta = tuple(a - b for a, b in zip(anchor, reference))
    return {"origin_max": center, "bottom_center_max": bottom, "delta_max": delta,
            "back_bottom_center_max": reference,
            "placed_origin_max": tuple(a + b for a, b in zip(center, delta))}


def camera_azimuth_deg(camera):
    """以 PF1 权威位置/目标推导 −180 至 180 度方位角，忽略界面提示值。"""
    if camera.get("frame") != "PF1":
        raise ValueError("只接受 PF1 相机")
    position, target = vector3(camera["position_mm"]), vector3(camera["target_mm"])
    dx, dz = position[0] - target[0], position[2] - target[2]
    if math.hypot(dx, dz) == 0:
        raise ValueError("相机与目标的水平位置重合，无法确定方位角")
    return math.degrees(math.atan2(dx, dz))


def should_hide_template_geometry(minimum, maximum, back_y_mm, azimuth_deg, mm_per_unit=1.0):
    """仅背面角度 abs > 100° 且几何整体位于背面平面之后时隐藏。"""
    low, _ = validate_bbox(minimum, maximum)
    plane, angle = float(back_y_mm), float(azimuth_deg)
    if not all(math.isfinite(value) for value in (plane, angle)):
        raise ValueError("背面平面和方位角必须为有限数值")
    return abs(angle) > 100.0 and units_to_mm(low[1], mm_per_unit) >= plane


_CORONA_ENTRY = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*)$")


def sample_rows(height, count=9):
    """在图像高度内均匀取若干行（不含最上最下边缘），用于抽查成图像素。"""
    height = int(height)
    if height <= 0:
        return []
    count = max(1, min(int(count), height))
    return sorted({min(height - 1, int(height * (i + 1) / (count + 1))) for i in range(count)})


def is_blank(samples, threshold=2.0):
    """抽样像素（r, g, b，0–255）全部不高于阈值时视为全黑；没有样本不判黑。"""
    seen = False
    for pixel in samples:
        seen = True
        if any(float(channel) > threshold for channel in pixel):
            return False
    return seen


def corona_error_since(log_path, started_epoch, tail_bytes=65536):
    """读取 Corona `*_errors.txt` 末尾，返回本次渲染开始之后记录的第一条错误（含随后几行说明）。

    Corona 按本机时间写 `YYYY-MM-DD HH:MM:SS  消息`，精度到秒，所以开始时间放宽 1 秒。
    """
    if not log_path:
        return None
    path = Path(log_path)
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - int(tail_bytes)))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _CORONA_ENTRY.match(line)
        if not match:
            continue
        try:
            when = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if when < float(started_epoch) - 1:
            continue
        detail = [match.group(2).strip()]
        for extra in lines[index + 1:index + 6]:
            if _CORONA_ENTRY.match(extra) or extra.startswith("Stack trace") or extra.startswith("Debugging"):
                break
            if extra.strip():
                detail.append(extra.strip())
        return " ".join(detail)
    return None


def blank_render_message(corona_error):
    """全黑成图时给使用者看的原因。"""
    text = corona_error or ""
    if "ran out of RAM" in text or "Bad allocation" in text:
        return ("Corona 内存不足，没能渲染出来：电脑内存（含虚拟内存）已被占满。"
                "请关掉占内存的程序或重启电脑后重新渲染。")
    if text:
        return "渲染结果全黑，Corona 报错：" + text[:300]
    return "渲染结果全黑，Corona 可能中途出错或被取消，请重新渲染。"


def smallshot_time_limit_ms(seconds):
    """暂按 Corona 时间属性使用毫秒；目标版本单位待实测。"""
    seconds = float(seconds)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("小样时间上限必须大于零且为有限数值")
    milliseconds = seconds * 1000
    if not math.isfinite(milliseconds):
        raise ValueError("小样时间上限超出可表示范围")
    return int(math.ceil(milliseconds))


def select_mask_elements(elements):
    """名称/类名精确匹配优先；模糊 ID 回退绝不选已知线框通道。"""
    elements = list(elements)
    ordered = sorted(elements, key=lambda element: (not element["enabled"], element["index"]))
    exact_id = [element for element in ordered if "遮罩_ID" in element["names"]]
    exact_wire = [element for element in ordered if "遮罩_线框颜色" in element["names"]]
    fuzzy = [element for element in ordered
             if "遮罩_线框颜色" not in element["names"]
             and any(token in " ".join(element["names"]).casefold() for token in ("mask", "id", "遮罩"))]
    selected = exact_id or fuzzy
    return {"id_index": selected[0]["index"] if selected else None,
            "wire_index": exact_wire[0]["index"] if exact_wire else None,
            "id_exact": bool(exact_id)}


def bbox_document(minimum, maximum, mesh_count, mm_per_unit=1.0):
    low, high = validate_bbox(minimum, maximum)
    if isinstance(mesh_count, bool) or not isinstance(mesh_count, int) or mesh_count < 0:
        raise ValueError("网格数量必须是非负整数")
    scale = unit_scale(mm_per_unit)
    low_mm, high_mm = ([v * scale for v in point] for point in (low, high))
    size = [b - a for a, b in zip(low_mm, high_mm)]
    return {"units": "mm", "max_world_bbox_mm": {"min": low_mm, "max": high_mm},
            "size_mm": {"x": size[0], "y_up": size[2], "z_depth": size[1]},
            "mesh_count": mesh_count}


def material_kinds(name="", class_name=""):
    """名称启发式识别，仅供日志，不据此改材质或渲染参数。"""
    text = "{} {}".format(name, class_name).casefold()
    result = []
    if any(token in text for token in ("glass", "玻璃", "glazing")):
        result.append("玻璃")
    if any(token in text for token in ("metal", "金属", "steel", "chrome", "alumin", "钢", "铝", "铜")):
        result.append("金属")
    return tuple(result)


# §11.4 白名单；未列出的字段不会传入 Max。
MATERIAL_LIMITS = {
    "hue_shift_deg": (-15, 15), "saturation_mul": (0.6, 1.4),
    "brightness_mul": (0.5, 2.5), "contrast_mul": (0.7, 1.3),
    "reflection_mul": (0, 2), "glossiness_delta": (-0.3, 0.3),
    "refraction_mul": (0, 1.5), "ior": (1.3, 1.8), "opacity_mul": (0.5, 1),
}
MATERIAL_PROPERTIES = {
    "legacy": {
        "color": ("colorDiffuse", "diffuseColor"),
        "map": ("texmapDiffuse", "diffuseTexmap"),
        "reflection_mul": ("levelReflect", "reflectionLevel", "reflectLevel"),
        "glossiness_delta": ("reflectGlossiness", "reflectionGlossiness", "reflGlossiness", "roughness"),
        "refraction_mul": ("levelRefract", "refractionLevel", "refractLevel"),
        "ior": ("IOR", "refractIor", "refractionIOR"),
        "opacity_mul": ("levelOpacity", "opacity", "opacityLevel"),
    },
    "physical": {
        "color": ("baseColor", "base_color"),
        "map": ("basetexmap", "baseTexmap", "texmapBase", "baseColorTexmap"),
        # 不用 baseLevel 代替反射：它会同时改变底色。
        "reflection_mul": ("baseIor", "baseIOR"),
        "glossiness_delta": ("baseroughness", "baseRoughness", "roughness", "baseGlossiness", "glossiness"),
        "refraction_mul": ("refractionamount", "refractionAmount", "refractionLevel", "refractLevel"),
        "ior": ("baseIor", "baseIOR", "ior"),
        "opacity_mul": ("opacityLevel", "opacity", "levelOpacity"),
    },
}
MATERIAL_PARAMETERS = {
    "legacy": (("colorDiffuse", "diffuseColor"), ("levelDiffuse",), ("levelReflect",),
               ("colorReflect",), ("reflectGlossiness",), ("fresnelIor",), ("levelRefract",),
               ("colorRefract",), ("refractGlossiness",), ("IOR", "refractIor", "refractionIOR"),
               ("levelOpacity", "opacity", "opacityLevel"), ("colorOpacity",)),
    "physical": (("baseColor", "base_color"), ("baseLevel",), ("baseroughness", "baseRoughness"),
                 ("roughnessMode", "baseRoughnessMode"), ("baseIor", "baseIOR"),
                 ("refractionamount", "refractionAmount"), ("opacityLevel",),
                 ("opacitycolor", "opacityColor"), ("metalnessMode",)),
}
COLOR_CORRECT_PROPERTIES = {
    # 2026-11-20 Claude 在中文 Max 2025 + Corona 12.2 实测「Corona色彩校正」：
    # inputTexmap、hue(0)、saturation(0，增量)、brightness(0，增量)、exposure(0，档)、contrast(1，倍率)、gamma(1)
    "input": ("inputTexmap", "inputMap", "texmap", "input", "map"),
    "hue_shift_deg": ("hue", "hueShift", "hue_shift"),
    "saturation_mul": ("saturation", "saturationMultiplier"),
    "brightness_mul": ("exposure", "brightness", "brightnessMultiplier"),
    "contrast_mul": ("contrast", "contrastMultiplier"),
}
# 色彩校正贴图的类：中文版没有英文全局名，要在 textureMap.classes 里按这些名字找
COLOR_CORRECT_CLASS_NAMES = ("CoronaColorCorrect", "Corona色彩校正")
LIGHT_INTENSITY_PROPERTIES = ("intensity", "multiplier", "intensityMultiplier")
REFLECT_MAP_PROPERTIES = ("bg_texmapReflect", "bg_texmapReflection", "reflectOverrideTexmap")


def clamp_number(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("调整值必须是有限数值")
    return max(minimum, min(maximum, float(value)))


LIGHTING_LIMITS = {"azimuth_deg": (-180, 180), "elevation_deg": (5, 85),
                   "intensity": (0.6, 1.5), "contrast": (0, 1)}
LIGHTING_DEFAULTS = {"azimuth_deg": None, "elevation_deg": 35,
                     "intensity": 1.0, "contrast": 0.5}
LIGHTING_TUNING_DEFAULTS = {
    "fill_min": 0.10, "fill_gamma": 1, "env_gi_min": 0.15,
    "key_size_max": 0.50, "key_size_min": 0.25, "key_distance": 2.5,
    "key_compensation": 111, "bg_fill_keep": 0,
}
ENV_GI_BINDINGS = {"gi_multiplier", "direct_enabled", "direct_map", "direct_color",
                   "source_map", "source_color", "solid_map_class", "solid_map_color"}
LIGHTING_BACKGROUND_NAMES = ("Rectangle2139486238", "Rectangle2139486305")
LIGHTING_TUNING_SWITCHES = ("probe", "capture_mask")
CORONA_LIGHT_CLASS_NAMES = ("CoronaLight", "Corona灯光", "CR灯光")


def normalize_lighting(value):
    """返回独立的截断参数；非法方位置 null，其他非法字段采用 §12 默认值。"""
    if value is None:
        return None
    result = dict(LIGHTING_DEFAULTS)
    if not isinstance(value, dict):
        return result
    for key, limits in LIGHTING_LIMITS.items():
        try:
            result[key] = clamp_number(value.get(key, result[key]), *limits)
        except ValueError:
            pass
    return result


def lighting_number(value, minimum=0, maximum=math.inf, *, positive=False):
    try:
        return (type(value) in (float, int) and math.isfinite(value)
                and minimum <= value <= maximum and (not positive or value > 0))
    except OverflowError:
        return False


def normalize_lighting_tuning(value):
    value = value if isinstance(value, dict) else {}
    tuning = dict(LIGHTING_TUNING_DEFAULTS)
    for key in tuning:
        maximum = 1 if key in {"fill_min", "env_gi_min", "bg_fill_keep"} else math.inf
        if lighting_number(value.get(key), maximum=maximum, positive=maximum != 1 and key != "key_compensation"):
            tuning[key] = value[key]
    if tuning["key_size_min"] > tuning["key_size_max"]:
        for key in ("key_size_min", "key_size_max"):
            tuning[key] = LIGHTING_TUNING_DEFAULTS[key]
    if lighting_number(value.get("key_intensity"), positive=True):
        tuning["key_intensity"] = value["key_intensity"]
    bindings = value.get("env_gi_properties")
    if (isinstance(bindings, dict) and bindings.keys() <= ENV_GI_BINDINGS
            and all(isinstance(name, str) and name.strip() == name and name for name in bindings.values())):
        tuning["env_gi_properties"] = dict(bindings)
    for key in LIGHTING_TUNING_SWITCHES:
        tuning[key] = value.get(key) is True
    return tuning


def lighting_output_stamp(path):
    try:
        stat = Path(path).stat()
        return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size) if S_ISREG(stat.st_mode) and stat.st_size else None
    except FileNotFoundError:
        return None


def lighting_fill_ratio(contrast, fill_min=LIGHTING_TUNING_DEFAULTS["fill_min"],
                        fill_gamma=LIGHTING_TUNING_DEFAULTS["fill_gamma"]):
    return 1.0 - (1.0 - fill_min) * clamp_number(contrast, 0, 1) ** fill_gamma


def lighting_response(lighting, tuning, base_intensity=LIGHTING_DEFAULT_KEY_INTENSITY, ai_mul=1.0):
    """补光、GI、主光共用曲线。尺寸与距离都以产品对角线为单位。"""
    contrast = lighting["contrast"]
    intensity = lighting["intensity"]
    base = tuning.get("key_intensity", base_intensity)
    if not lighting_number(base, positive=True):
        raise ValueError("config.key_light_intensity 必须为有限正数")
    fill = lighting_fill_ratio(contrast, tuning["fill_min"], tuning["fill_gamma"])
    loss = 1.0 - fill
    strength = base * intensity * ai_mul * (1.0 + tuning["key_compensation"] * loss)
    if not math.isfinite(strength):
        raise ValueError("主光强度超出有限数值范围")
    size_max, size_min = tuning["key_size_max"], tuning["key_size_min"]
    return {
        "fill_ratio": fill, "bg_fill_ratio": fill + loss * tuning["bg_fill_keep"],
        "env_gi": 1.0 - (1.0 - tuning["env_gi_min"]) * contrast,
        "key_size": size_max - (size_max - size_min) * contrast if contrast < 1 else size_min,
        "key_distance": tuning["key_distance"], "key_strength": strength,
    }


def ray_exit_distance(origin, direction, low, high):
    """从包围盒内一点沿方向走出盒子的距离；起点不在盒内或方向为零时返回 None。"""
    exit_t = math.inf
    for o, d, lo, hi in zip(origin, direction, low, high):
        if not lo <= o <= hi:
            return None
        if d > 1e-12:
            exit_t = min(exit_t, (hi - o) / d)
        elif d < -1e-12:
            exit_t = min(exit_t, (lo - o) / d)
    return None if math.isinf(exit_t) else exit_t


def clamp_key_distance(distance, center, direction, room, margin):
    """主光不能放到模板布景（墙、地、侧板）外面，否则会被布景挡住。

    2026-11-21 实测：111(2022) 右前 45° 时主光按 2.5×对角线放到 x=2761，模板布景右边界 2631，
    整张图几乎全黑。这里先把布景范围四周各缩进 margin，再沿光线算出盒距离，保证灯到每面墙的
    垂直距离都不小于 margin；产品太靠边、缩进后不在盒内时，退到原布景出盒距离的一半。
    """
    if room is None:
        return distance
    low = tuple(v + margin for v in room[0])
    high = tuple(v - margin for v in room[1])
    exit_t = ray_exit_distance(center, direction, low, high) if all(a < b for a, b in zip(low, high)) else None
    if exit_t is not None and exit_t > 0:
        return min(distance, exit_t)
    exit_t = ray_exit_distance(center, direction, room[0], room[1])
    if exit_t is None or exit_t <= 0:
        return distance
    return min(distance, 0.5 * exit_t)


def key_distance_falloff(distance, nominal_distance):
    """主光被收近后保持产品处照度不变的强度倍率（距离平方反比）。"""
    if not (lighting_number(distance, positive=True) and lighting_number(nominal_distance, positive=True)):
        return 1.0
    return min(1.0, (distance / nominal_distance) ** 2)


def lighting_ray_box_distance(origin, direction, bounds):
    """返回灯光轴线进入世界包围盒的距离参数；允许墙和地面为零厚度平面。"""
    near, far = 0.0, math.inf
    for pos, axis, low, high in zip(origin, direction, *bounds):
        if abs(axis) < 1e-9:
            if not low <= pos <= high:
                return math.inf
            continue
        a, b = (low - pos) / axis, (high - pos) / axis
        near, far = max(near, min(a, b)), min(far, max(a, b))
        if far < near:
            return math.inf
    return near


def lighting_position_max(azimuth_deg, elevation_deg, distance, center_max=(0, 0, 0)):
    """PF1 光源球坐标 → Max 位置；距离和中心使用相同的 Max 系统单位。"""
    az = math.radians(clamp_number(azimuth_deg, -180, 180))
    el = math.radians(clamp_number(elevation_deg, 5, 85))
    if isinstance(distance, bool) or not isinstance(distance, (int, float)) or not math.isfinite(distance) or distance <= 0:
        raise ValueError("主光距离必须为有限正数")
    horizontal = distance * math.cos(el)
    # PF1 +X → Max +X，PF1 +Z（正面）→ Max −Y，PF1 +Y → Max +Z。
    offset = (horizontal * math.sin(az), -horizontal * math.cos(az), distance * math.sin(el))
    return tuple(a + b for a, b in zip(vector3(center_max), offset))


def is_corona_light_class_name(name):
    name = str(name).casefold()
    marker = "corona" in name or bool(re.search(r"(?<![a-z])cr(?=light|灯|[^a-z]|$)", name))
    return name in {candidate.casefold() for candidate in CORONA_LIGHT_CLASS_NAMES} or (
        marker and ("灯" in name or "light" in name))


def lighting_description(lighting):
    """§12.2 的中文描述；清单不依赖服务端附带的文字。"""
    lighting = normalize_lighting(lighting)
    if lighting is None or lighting["azimuth_deg"] is None:
        return "模板默认灯光"
    az, el, contrast = (lighting[key] for key in ("azimuth_deg", "elevation_deg", "contrast"))
    side = "左" if az < 0 else "右"
    direction = ("正前方" if abs(az) < 20 else side + "前方" if abs(az) < 70
                 else side + "侧" if abs(az) <= 110 else side + "后方（逆光）")
    height = "低角度" if el < 20 else "斜上方" if el <= 55 else "高处（接近顶光）"
    tone = "柔和" if contrast < 0.3 else "中等" if contrast <= 0.7 else "强烈"
    faces = ("正面受光、两侧略暗" if abs(az) < 20 else
             "{}侧受光较亮、{}侧偏暗".format(side, "右" if az < 0 else "左"))
    if el > 55:
        faces += "，顶面最亮"
    return "主光从{}{}照来（水平 {:g}°，高 {:g}°）；{}；明暗对比{}。".format(
        direction, height, az, el, faces, tone)


def _object(value):
    if not isinstance(value, dict):
        raise ValueError("调整分组必须是对象")
    return value


def clamp_overrides(overrides):
    """返回新对象；null 等于无调整，未知字段丢弃，非法数值明确报错。"""
    source = {} if overrides is None else _object(overrides)
    result = {"materials": {}, "lights": {"intensity_mul": 1.0, "rotate_deg": 0.0, "per_light": {}},
              "environment": {"reflection_strength_mul": 1.0}, "items": {}}
    for name, values in _object(source.get("materials", {})).items():
        result["materials"][str(name)] = {
            key: (None if key == "ior" and value is None else clamp_number(value, *MATERIAL_LIMITS[key]))
            for key, value in _object(values).items() if key in MATERIAL_LIMITS}
    lights = _object(source.get("lights", {}))
    for key, limits in (("intensity_mul", (0.5, 1.6)), ("rotate_deg", (-90, 90))):
        if key in lights:
            result["lights"][key] = clamp_number(lights[key], *limits)
    for name, values in _object(lights.get("per_light", {})).items():
        values = _object(values)
        if "intensity_mul" in values:
            result["lights"]["per_light"][str(name)] = {"intensity_mul": clamp_number(values["intensity_mul"], 0.5, 2)}
    environment = _object(source.get("environment", {}))
    if "reflection_strength_mul" in environment:
        result["environment"]["reflection_strength_mul"] = clamp_number(environment["reflection_strength_mul"], 0.5, 2)
    for no, values in _object(source.get("items", {})).items():
        item_lights = _object(_object(values).get("lights", {}))
        if "rotate_deg" in item_lights:
            result["items"][str(no)] = {"lights": {"rotate_deg": clamp_number(item_lights["rotate_deg"], -90, 90)}}
    return result


def item_light_angle(overrides, no):
    return overrides["items"].get(str(no), {}).get("lights", {}).get("rotate_deg", overrides["lights"]["rotate_deg"])


def property_checker(available):
    if callable(available):
        return available
    if isinstance(available, (set, list, tuple, dict)):
        return lambda name: name in available
    return lambda name: hasattr(available, name)


def material_family(available):
    """仅按属性识别，兼容中文 Max；也接受只有属性和值的离线假材质。"""
    exists = property_checker(available)
    if any(exists(p) for p in ("giMtlOn", "reflectMtlOn", "refractMtlOn", "directMtlOn")):
        return "ray_switch"
    if exists("materialList"):
        return "multi"
    if any(exists(p) for p in ("baseColor", "base_color", "basetexmap", "baseTexmap", "baseIor",
                               "baseroughness", "baseRoughness", "refractionamount", "refractionAmount")):
        return "physical"
    if any(exists(p) for p in ("colorDiffuse", "texmapDiffuse", "levelDiffuse", "levelReflect",
                               "reflectGlossiness", "levelRefract", "refractIor", "levelOpacity")):
        return "legacy"
    return None


def pick_property(candidates, available):
    """按候选顺序选第一个；available 可为 hasProperty 回调或属性名集合。"""
    exists = property_checker(available)
    return next((name for name in candidates if exists(name)), None)


def material_property(available, adjustment):
    return pick_property(MATERIAL_PROPERTIES.get(material_family(available), {}).get(adjustment, ()), available)


def material_parameters(available):
    return [prop for candidates in MATERIAL_PARAMETERS.get(material_family(available), ())
            for prop in [pick_property(candidates, available)] if prop is not None]


def ray_switch_children(material, names=None, getter=getattr, identity=id):
    """direct 优先；只收集材质槽，按身份去重，不参考开关值。"""
    names = list(dir(material) if names is None else names)
    names = sorted((str(n) for n in names if str(n).casefold().endswith("mtl")),
                   key=lambda n: n.casefold() != "directmtl")
    result, seen = [], set()
    for name in names:
        child = getter(material, name)
        if child is not None and identity(child) not in seen:
            result.append(child)
            seen.add(identity(child))
    return result


def dominant_submaterial(materials, material_ids, face_ids):
    """按实际面 ID 计数，不能用槽序号代替 materialIDList；空槽也不能替换。"""
    counts = {}
    for face_id in face_ids:
        face_id = int(face_id)
        counts[face_id] = counts.get(face_id, 0) + 1
    if not counts:
        return None
    dominant = max(counts, key=lambda value: (counts[value], -value))
    return next((mat for mat, mat_id in zip(materials, material_ids) if int(mat_id) == dominant), None)


def physical_reflection_ior(current_ior, multiplier):
    """F0 乘系数后反算 n；保持底色强度，F0 ≤ 0.2，n ≥ 1.01。"""
    n = max(1.01, float(current_ior))
    f0 = min(0.2, max(0.0, ((n-1)/(n+1))**2 * multiplier))
    root = math.sqrt(f0)
    return max(1.01, (1+root)/(1-root))


def adjusted_scalar(current, adjustment, value, property_name, *, glossiness_mode=False):
    if adjustment == "reflection_mul" and property_name.casefold() == "baseior":
        return physical_reflection_ior(current, value)
    if adjustment == "ior":
        return clamp_number(value, *MATERIAL_LIMITS["ior"])
    if adjustment == "glossiness_delta":
        roughness = "rough" in property_name.casefold() and not glossiness_mode
        return clamp_number(float(current) + (-value if roughness else value), 0, 1)
    return clamp_number(float(current) * value, 0, 1)


def adjust_hsv(rgb, hue_shift_deg=0, saturation_mul=1, brightness_mul=1, contrast_mul=1):
    """RGB 为 Max Color 的 0…255 数值；HSV 改色后以 0.5 为中心做对比度。"""
    channels = [clamp_number(v, 0, 255) / 255 for v in vector3(rgb)]
    h, s, v = colorsys.rgb_to_hsv(*channels)
    h = (h + clamp_number(hue_shift_deg, -15, 15) / 360) % 1
    s = min(1, s * clamp_number(saturation_mul, 0.6, 1.4))
    v = min(1, v * clamp_number(brightness_mul, *MATERIAL_LIMITS["brightness_mul"]))
    contrast = clamp_number(contrast_mul, 0.7, 1.3)
    return tuple(max(0, min(255, ((c - 0.5) * contrast + 0.5) * 255))
                 for c in colorsys.hsv_to_rgb(h, s, v))


def color_correct_value(adjustment, value, neutral, prop=None):
    """兼容校正贴图的中性值 0（增量）或 1（倍率）；其他尺度拒绝猜测。
    亮度优先写到 exposure（曝光档）：倍率 m 换算为 log2(m) 档，按倍数提亮不洗白。"""
    if adjustment == "hue_shift_deg":
        return value
    if prop is not None and str(prop).lower() == "exposure":
        if value <= 0:
            raise ValueError("亮度倍率必须大于 0")
        return math.log2(value)
    if neutral == 0:
        return value - 1
    if neutral == 1:
        return value
    raise ValueError("色彩校正属性的中性值不是 0 或 1，无法确定尺度")


def rotation_z_matrix(degrees):
    angle = float(degrees)
    if not math.isfinite(angle):
        raise ValueError("旋转角度必须为有限数值")
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    # Max Matrix3 使用行向量，正角度从上往下看逆时针。
    return ((c, s, 0), (-s, c, 0), (0, 0, 1))


def rotate_z(vector, degrees):
    vector = vector3(vector)
    matrix = rotation_z_matrix(degrees)
    return tuple(sum(vector[i] * matrix[i][j] for i in range(3)) for j in range(3))


def rotate_transform_z(rows, center, degrees):
    if len(rows) != 4:
        raise ValueError("Max 变换矩阵必须有四行")
    center, position = vector3(center), vector3(rows[3])
    offset = rotate_z(tuple(p - c for p, c in zip(position, center)), degrees)
    return tuple(rotate_z(row, degrees) for row in rows[:3]) + (tuple(c + p for c, p in zip(center, offset)),)


def material_kind(name="", class_name="", has_bitmap=False, has_color=True):
    text = "{} {}".format(name, class_name).casefold()
    if any(word in text for word in ("玻璃", "glass", "长虹")):
        return "glass"
    if any(word in text for word in ("金属", "五金", "metal", "chrome", "金色", "银色")):
        return "metal"
    if has_bitmap and any(word in text for word in ("木", "橡", "胡桃", "oak", "walnut")):
        return "wood"
    return "solid" if has_color and not has_bitmap else "other"


def region_palette(count):
    """在 RGB 候选网格中选最远颜色；相同数量生成相同结果，远离黑白。"""
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("材质数量必须是非负整数")
    candidates = [(r, g, b) for r in range(32, 225, 32) for g in range(32, 225, 32)
                  for b in range(32, 225, 32) if max(r, g, b) - min(r, g, b) >= 64]
    if count > len(candidates):
        raise ValueError("材质数量超过可区分配色容量：{}".format(len(candidates)))
    def distance(a, b):
        return sum((x - y) ** 2 for x, y in zip(a, b))
    distances = {color: min(distance(color, (0, 0, 0)), distance(color, (255, 255, 255))) for color in candidates}
    result = []
    for _ in range(count):
        chosen = max(distances, key=distances.get)
        result.append(chosen)
        del distances[chosen]
        for color in distances:
            distances[color] = min(distances[color], distance(color, chosen))
    return result


def calibration_directory(task_dir, round_no=0):
    if isinstance(round_no, bool) or not isinstance(round_no, int) or round_no < 0:
        raise ValueError("calibration_round 必须为非负整数")
    return Path(task_dir) / "calibration" / "round-{}".format(round_no)
