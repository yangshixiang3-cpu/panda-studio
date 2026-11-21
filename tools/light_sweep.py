"""串行提交小样并从遮罩 ID 统计亮度，供 Corona 打光实测。"""
import argparse
from contextlib import contextmanager
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "max"))
from wb_core import (LIGHTING_LIMITS, LIGHTING_TUNING_SWITCHES, lighting_number,
                     lighting_output_stamp, normalize_lighting, normalize_lighting_tuning)


DEFAULT_TASK = "20261120-235812-366c"
CONTACT_SHEET = Path(r"C:\Users\<用户名>\.claude\skills\claude-codex-collab\scripts\contact-sheet.ps1")
DEFAULT_CASES = (
    ("template", None, 35, .5),
    ("left87-low-023", -87, 15, .23),
    ("left12-high-023", -12, 40, .23),
    ("left12-high-100", -12, 40, 1),
    ("left60-000", -60, 35, 0),
    ("left60-025", -60, 35, .25),
    ("left60-050", -60, 35, .5),
    ("left60-100", -60, 35, 1),
    ("right60-100", 60, 35, 1),
)
METRIC_NAMES = ("right_left_ratio", "left_mean", "right_mean", "left_pixels", "right_pixels",
                "background_mean", "product_mean", "face_ratio", "bright_face_mean",
                "dark_face_mean", "bright_face_pixels", "dark_face_pixels",
                "legacy_id_metric_error", "metric_error")
TERMINAL = {"done", "failed"}
COLOR_NEIGHBORS = tuple((r, g, b) for r in range(-2, 3) for g in range(-2, 3) for b in range(-2, 3)
                        if r * r + g * g + b * b <= 4)


def parse_config(content):
    value = json.loads(content.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("config.json 必须是 JSON 对象")
    return value


def read_config(path):
    return parse_config(path.read_bytes())


def validate_tuning(value):
    if not isinstance(value, dict):
        raise ValueError("lighting_tuning 必须是 JSON 对象")
    normalized = normalize_lighting_tuning(value)
    for key, setting in value.items():
        expected = normalized.get(key)
        if key not in normalized or setting != expected or (
                key not in {"env_gi_properties", *LIGHTING_TUNING_SWITCHES} and not lighting_number(setting)):
            raise ValueError("lighting_tuning.{} 值无效：{}".format(key, setting))
        if key in LIGHTING_TUNING_SWITCHES and type(setting) is not bool:
            raise ValueError("lighting_tuning.{} 必须是布尔值".format(key))
    return value


def validate_light(value):
    if value is None:
        value = {"azimuth_deg": None}
    if not isinstance(value, dict) or not value.keys() <= LIGHTING_LIMITS.keys() or "azimuth_deg" not in value:
        raise ValueError("lighting 必须为 null 或包含 azimuth_deg 的打光对象")
    for key, setting in value.items():
        if key == "azimuth_deg" and setting is None:
            continue
        if not lighting_number(setting, *LIGHTING_LIMITS[key]):
            raise ValueError("lighting.{} 值无效：{}".format(key, setting))
    return normalize_lighting(value)


def make_cases(config, path=None):
    base = validate_tuning(config.get("lighting_tuning", {}))
    if not lighting_number(config.get("key_light_intensity", 20), positive=True):
        raise ValueError("key_light_intensity 必须为有限正数")
    if path is None:
        document = [{"name": name, "lighting": {"azimuth_deg": az, "elevation_deg": el,
                                               "contrast": contrast, "intensity": 1}}
                    for name, az, el, contrast in DEFAULT_CASES]
    else:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, list) or not document:
        raise ValueError("组合文件必须是非空 JSON 数组")
    cases = []
    names = set()
    for case in document:
        if (not isinstance(case, dict) or not {"name", "lighting"} <= case.keys()
                or not case.keys() <= {"name", "lighting", "lighting_tuning"}):
            raise ValueError("每个组合须包含 name、lighting，可附 lighting_tuning")
        name = case["name"]
        if (not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", name)
                or name.casefold() in names):
            raise ValueError("组合名称必须唯一，并使用 1–48 个英文字母、数字、连字符或下划线：{}".format(name))
        names.add(name.casefold())
        overrides = case.get("lighting_tuning", {})
        if not isinstance(overrides, dict):
            raise ValueError("{} 的 lighting_tuning 必须是对象".format(name))
        tuning = validate_tuning({**base, **overrides})
        cases.append({"name": name, "lighting": validate_light(case["lighting"]),
                      "lighting_tuning": {**tuning, "capture_mask": True}})
    return cases


def atomic_config_write(path, content):
    temp = path.with_name(".lighting-{}.tmp".format(uuid4().hex))
    with temp.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(30):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(.02)


def config_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


@contextmanager
def temporary_tuning(path):
    original_bytes = path.read_bytes()
    original = parse_config(original_bytes)
    changed = False

    def set_tuning(value):
        nonlocal changed
        current = read_config(path)
        current["lighting_tuning"] = value
        atomic_config_write(path, config_bytes(current))
        changed = True

    try:
        yield set_tuning
    finally:
        if changed:
            current = read_config(path)
            if "lighting_tuning" in original:
                current["lighting_tuning"] = original["lighting_tuning"]
            else:
                current.pop("lighting_tuning", None)
            content = config_bytes(current)
            if content == config_bytes(original):
                content = original_bytes
            atomic_config_write(path, content)


def request(base, path, method="GET", data=None):
    req = Request(base + path, method=method, data=None if data is None else config_bytes(data),
                  headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urlopen(req, timeout=10) as response:
            return json.load(response)
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("接口失败 {} {}：HTTP {} {}".format(method, path, exc.code, message)) from exc


def wait_for_job(base, endpoint, job_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        payload = request(base, endpoint)
        job = next((job for job in payload["jobs"] if job["job_id"] == job_id), None)
        if job and job["state"] in TERMINAL:
            item = next((item for item in job["items"] if item["no"] == 1), None)
            if job["state"] != "done" or not item or item["state"] != "done":
                raise RuntimeError("作业 {} 失败：{} {}".format(job_id, job.get("message", ""),
                                                              (item or {}).get("message", "")))
            return job, item
        if time.monotonic() >= deadline:
            raise TimeoutError("等待作业 {} 超时".format(job_id))
        time.sleep(.75)


def image_metrics(image_path, mask_path):
    from PIL import Image

    with Image.open(image_path) as image, Image.open(mask_path) as mask:
        if image.size != mask.size:
            raise ValueError("小样与遮罩的像素尺寸不同")
        width, height = image.size
        luminance, colors = image.convert("L"), mask.convert("RGB")
    top_pixels = width * max(1, height // 5)
    third = width // 3
    bins = {}
    product_count = product_sum = background_count = background_sum = 0
    left_count = left_sum = right_count = right_sum = 0
    channels = [channel.tobytes() for channel in colors.split()]
    for index, (level, red, green, blue) in enumerate(zip(luminance.tobytes(), *channels)):
        # 遮罩为 JPEG，近黑视为黑，16 级色桶归并同一 ID 的压缩颜色扰动。
        if max(red, green, blue) < 24:
            if index < top_pixels:
                background_count += 1
                background_sum += level
            continue
        product_count += 1
        product_sum += level
        x = index % width
        if x < third:
            left_count += 1
            left_sum += level
        elif x >= width - third:
            right_count += 1
            right_sum += level
        key = (red // 16, green // 16, blue // 16)
        bucket = bins.setdefault(key, [0, 0])
        bucket[0] += 1
        bucket[1] += level
    if not product_count or not background_count:
        raise ValueError("遮罩中缺少非黑产品区域，或画面上部 20% 没有黑色背景")

    regions = []
    for dominant in sorted(bins, key=lambda key: bins[key][0], reverse=True):
        if dominant not in bins:
            continue
        red, green, blue = dominant
        count = total = 0
        for dr, dg, db in COLOR_NEIGHBORS:
            pixels, levels = bins.pop((red + dr, green + dg, blue + db), (0, 0))
            count += pixels
            total += levels
        if count >= max(1, product_count * .01):
            regions.append((count, total / count))
    left = left_sum / left_count if left_count else None
    right = right_sum / right_count if right_count else None
    metrics = {"background_mean": background_sum / background_count,
               "product_mean": product_sum / product_count,
               "left_mean": left, "right_mean": right,
               "left_pixels": left_count, "right_pixels": right_count,
               "metric_error": ""}
    if left is None or right is None:
        metrics["metric_error"] = "画面左侧或右侧三分之一缺少产品像素"
    elif left == 0:
        metrics["metric_error"] = "画面左侧三分之一产品均值为零，无法计算右／左比值"
    else:
        metrics["right_left_ratio"] = right / left
    if len(regions) < 2:
        return {**metrics, "legacy_id_metric_error": "遮罩未找到两个独立的主要颜色 ID"}
    largest = sorted(regions, reverse=True)[:2]
    bright, dark = sorted(((mean, count) for count, mean in largest), reverse=True)
    ratio = bright[0] / dark[0] if dark[0] else float("inf") if bright[0] else None
    return {**metrics, "face_ratio": ratio, "bright_face_mean": bright[0], "dark_face_mean": dark[0],
            "bright_face_pixels": bright[1], "dark_face_pixels": dark[1],
            "legacy_id_metric_error": "两块主要区域的平均亮度均为零" if ratio is None else ""}


def render_seconds(job, item):
    try:
        return (datetime.fromisoformat(job["updated_at"]) - datetime.fromisoformat(item["started_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def run_sweep(args, cases, base, endpoint, output):
    health = request(base, "/api/health")
    if (health.get("service") != "whitebg-studio" or args.config.name.casefold() != "config.json"
            or Path(health["root"]).resolve() != args.config.parent):
        raise RuntimeError("服务项目目录与 config.json 所在目录不同")
    bundle = request(base, endpoint)
    task = bundle["task"]
    if [item["no"] for item in task["items"] if item["enabled"]] != [1]:
        raise ValueError("请使用仅启用第 1 项的测试任务")
    calibration = task.get("ai_calibration", {})
    if calibration.get("state") in {"rendering", "thinking"} or calibration.get("accepted_overrides") is not None:
        raise ValueError("请选择没有进行中或已采纳 AI 校准的测试任务")
    if any(job["state"] not in TERMINAL and job.get("type") == "render" for job in bundle["jobs"]):
        raise RuntimeError("测试任务仍有在途渲染作业")
    # 所有组合都写入第 1 项的单张设置，其他项不会随产品统一光变化。
    item = next(item for item in task["items"] if item["no"] == 1)
    mask = Path(bundle["task_dir"]) / "smallshots" / "_extra" / "1_遮罩_ID.jpg"
    renders, masks = output / "renders", output / "masks"
    output.mkdir(parents=True, exist_ok=False)
    renders.mkdir()
    masks.mkdir()
    incomplete = False
    enabled = calibration.get("enabled") is True
    try:
        if enabled:
            request(base, endpoint, "PUT", {"ai_calibration": {"enabled": False}})
        with temporary_tuning(args.config) as set_tuning, (output / "metrics.csv").open(
                "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, ("case", "job_id", "azimuth_deg", "elevation_deg", "intensity",
                                            "contrast", "lighting_tuning", "render_seconds", *METRIC_NAMES))
            writer.writeheader()
            for index, case in enumerate(cases, 1):
                label = "{:02d}-{}".format(index, case["name"])
                print("提交小样 {}".format(label), flush=True)
                # 2026-11-21 起打光要勾「光方向」才生效：有方向的用例显式打开
                item["lighting"] = {**case["lighting"], "enabled": case["lighting"]["azimuth_deg"] is not None}
                request(base, endpoint, "PUT", {"items": task["items"]})
                before_mask = lighting_output_stamp(mask)
                set_tuning(case["lighting_tuning"])
                job = request(base, endpoint + "/jobs", "POST", {"mode": "smallshot"})["job"]
                if job.get("mode") != "smallshot" or job.get("lighting_tuning") != case["lighting_tuning"]:
                    raise RuntimeError("作业 {} 没有返回本组的打光参数快照".format(job.get("job_id")))
                status, item_status = wait_for_job(base, endpoint, job["job_id"], args.timeout)
                image_path = renders / (label + ".jpg")
                mask_path = masks / (label + "_遮罩_ID.jpg")
                shutil.copy2(job["items"][0]["out_image"], image_path)
                after_mask = lighting_output_stamp(mask)
                if not after_mask or after_mask == before_mask:
                    metrics = {"metric_error": "作业 {} 没有生成新的遮罩 {}".format(job["job_id"], mask)}
                else:
                    shutil.copy2(mask, mask_path)
                    try:
                        metrics = image_metrics(image_path, mask_path)
                    except (OSError, ValueError) as exc:
                        metrics = {"metric_error": str(exc)}
                incomplete |= bool(metrics.get("metric_error"))
                if metrics.get("metric_error"):
                    print("{}：{}".format(label, metrics["metric_error"]), file=sys.stderr, flush=True)
                writer.writerow({"case": label, "job_id": job["job_id"], **case["lighting"],
                                 "lighting_tuning": json.dumps(case["lighting_tuning"], ensure_ascii=False),
                                 "render_seconds": render_seconds(status, item_status), **metrics})
                stream.flush()
    finally:
        if enabled:
            request(base, endpoint, "PUT", {"ai_calibration": {"enabled": True}})
    subprocess.run(["powershell.exe", "-NoProfile", "-File", str(CONTACT_SHEET),
                    "-Path", str(renders), "-Out", str(output / "contact-sheet.jpg")], check=True)
    print("结果目录 {}".format(output))
    return int(incomplete)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default=DEFAULT_TASK, help="仅启用第 1 项的测试任务 ID")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json", help="服务项目的 config.json")
    parser.add_argument("--cases", type=Path, help="自定义组合的 JSON 文件")
    parser.add_argument("--port", type=int, default=8770, help="本机白底服务端口")
    parser.add_argument("--timeout", type=float, default=300, help="每份作业等待秒数")
    parser.add_argument("--out", type=Path, help="结果目录，须为新目录")
    parser.add_argument("--dry-run", action="store_true", help="校验并打印组合")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", args.task):
        parser.error("任务 ID 格式无效")
    if not 1 <= args.port <= 65535 or not lighting_number(args.timeout, positive=True):
        parser.error("端口或等待时长无效")
    args.config = args.config.resolve()
    output = (args.out or ROOT / "exchange" / "light-sweeps" /
              ("{}-{}".format(args.task, datetime.now().strftime("%Y%m%d-%H%M%S-%f")))).resolve()
    base, endpoint = "http://127.0.0.1:{}".format(args.port), "/api/tasks/" + args.task
    try:
        cases = make_cases(read_config(args.config), args.cases)
        if args.dry_run:
            print(json.dumps({"task": args.task, "service": base, "output": str(output), "cases": cases},
                             ensure_ascii=False, indent=2))
            return 0
        if not CONTACT_SHEET.is_file():
            raise FileNotFoundError("找不到拼图脚本 {}".format(CONTACT_SHEET))
        return run_sweep(args, cases, base, endpoint, output)
    except (OSError, TypeError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as exc:
        print("打光扫参失败：{}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
