"""在 3ds Max 2025 主线程运行；场景和模板只读，不保存场景文件。"""

from __future__ import annotations

import json
import math
import sys
import time
import traceback
import types
from contextlib import contextmanager
from pathlib import Path

import pymxs
from pymxs import runtime as rt
from PySide6 import QtCore, QtWidgets

# python.ExecuteFile 并不等同于 import；允许从任意当前工作目录启动。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import wb_core as core


LOCK_SCRIPT = r"C:\Users\<用户名>\.codex\skills\panel-furniture-modeling\scripts\pf_max_lock.ms"
_SESSION_KEY = "_whitebg_studio_runner_session_v01"
RUNNER_VERSION = "0.2"
if _SESSION_KEY not in sys.modules:
    session = types.ModuleType(_SESSION_KEY)
    session.timer = None
    session.runner = None
    session.busy = False
    sys.modules[_SESSION_KEY] = session
_session = sys.modules[_SESSION_KEY]


class SceneRestoreError(RuntimeError):
    """临时场景状态无法恢复时停止本任务，避免污染下一项。"""


class BlankRenderError(RuntimeError):
    """渲染返回了全黑成图（如 Corona 内存不足中途放弃）；消息直接给使用者看。"""


def _ms_string(value):
    """普通 MAXScript 字符串转义，路径和 owner 均不能插入脚本语句。"""
    return json.dumps(str(value), ensure_ascii=False)


def _main_thread():
    app = QtWidgets.QApplication.instance()
    if app is None or QtCore.QThread.currentThread() != app.thread():
        raise RuntimeError("执行器必须在 Max 的 Qt 主线程运行")
    return app


class Runner:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self.config = core.load_config(self.config_path)
        self.exchange = Path(self.config["exchange_dir"])
        self.job = None
        self.status = None
        self.status_path = None
        self.owner = None
        self.heartbeat = None

    def log(self, message):
        """日志写入失败时保留内存状态并停在可诊断的 stderr，不弹窗。"""
        if self.status is not None:
            core.add_status_log(self.status, message)
        self.log_file(message)

    def log_file(self, message):
        try:
            core.append_runner_log(self.exchange, message)
        except Exception:
            sys.stderr.write("白底执行器无法写入 runner.log：" + traceback.format_exc() + "\n")

    def flush(self):
        core.write_status(self.status_path, self.status)

    def _scan_error(self, path, exc):
        message = "无法读取队列文件 {}：{}".format(path.name, exc)
        self.log(message)
        # 损坏的状态也终结为 failed，避免把可能已渲染的任务误当成新任务。
        status = core.new_status({"job_id": path.stem})
        status.update(state="failed", message=message)
        core.add_status_log(status, message)
        core.write_status(path.with_name(path.stem + ".status.json"), status)

    def process_next(self):
        self.job = self.status = self.status_path = self.owner = None
        queued = core.next_job(self.exchange / "jobs", self._scan_error)
        if queued is None:
            return None
        self.job = queued.data
        self.status_path = queued.status_path
        acquired = False
        lock_attempted = False
        scene_touched = False
        quiet_previous = None
        try:
            self.status = core.new_status(self.job)
            task_id = str(self.job["task_id"])
            if not task_id or task_id in (".", "..") or any(v in task_id for v in ("/", "\\", ":")):
                raise ValueError("task_id 必须是单层目录名")
            self.owner = "{}:{}".format(self.config["max_lock_owner_prefix"], self.job["job_id"])
            task_dir = self.exchange / "tasks" / task_id
            lock_attempted = True
            answer = str(rt.execute(
                '(fileIn @"{}"; ::PFMaxLock.acquire {} {})'.format(
                    LOCK_SCRIPT, _ms_string(self.owner), _ms_string(task_dir))))
            if not answer.startswith("OK"):
                self.status.update(state="waiting_lock", message=answer)
                self.log("等待 Max 锁：" + answer)
                return self.status
            acquired = True
            if len(list(rt.objects)) > 0 and rt.getSaveRequired():
                self.status.update(state="waiting_lock", message="当前 Max 场景有未保存的修改")
                self.log(self.status["message"])
                return self.status
            self.status.update(state="running", started_at=core.timestamp(), message="正在处理任务")
            if self.heartbeat is not None:
                self.heartbeat.set_job(self.job["job_id"])
            self.log("开始任务 {}（{}）".format(self.job["job_id"], self.job["type"]))
            self.flush()
            quiet_previous = rt.SetQuietMode(True)
            scene_touched = True  # load 失败也可能留下部分场景，必须清场。
            with pymxs.animate(False), pymxs.attime(0):
                if self.job["type"] == "export_preview":
                    self.export_preview()
                elif self.job["type"] == "render":
                    self.render_job()
                else:
                    raise ValueError("未知任务类型：{}".format(self.job["type"]))
            failed = any(item["state"] == "failed" for item in self.status["items"])
            self.status.update(state="failed" if failed else "done",
                               message="任务有明细失败，详见 items" if failed else "任务完成")
            self.log(self.status["message"])
        except core.VariantNodesNotFound as error:
            self.fail(str(error))
        except Exception:
            self.fail("任务异常：" + traceback.format_exc())
        finally:
            if acquired and scene_touched:
                try:
                    if rt.resetMaxFile(rt.Name("noPrompt")) is False:
                        raise RuntimeError("resetMaxFile 返回 false")
                    self.log("已清空 Max 场景")
                except Exception:
                    self.fail("清场失败：" + traceback.format_exc())
            # 即使 acquire 抛异常，也按同一 owner 尝试 release；从不 force 释放别人的锁。
            if lock_attempted:
                try:
                    answer = str(rt.execute("::PFMaxLock.release {}".format(_ms_string(self.owner))))
                    if acquired and not answer.startswith("OK"):
                        raise RuntimeError(answer)
                    if acquired:
                        self.log("已释放 Max 锁：" + answer)
                except Exception:
                    self.fail("释放 Max 锁失败：" + traceback.format_exc())
            if quiet_previous is not None:
                try:
                    rt.SetQuietMode(quiet_previous)
                except Exception:
                    self.fail("恢复安静模式失败：" + traceback.format_exc())
            if self.status is not None:
                try:
                    self.flush()
                except Exception:
                    self.log("状态文件写入失败：" + traceback.format_exc())
            if self.heartbeat is not None and self.heartbeat.job_id is not None:
                self.heartbeat.set_job(None)
        return self.status

    def fail(self, message):
        if self.status is None:
            self.status = core.new_status({"job_id": self.job["job_id"]})
        self.status.update(state="failed", message=message)
        for item in self.status["items"]:
            if item["state"] in ("pending", "rendering"):
                item.update(state="failed", message=message)
        self.log(message)

    def _touch(self):
        answer = str(rt.execute("::PFMaxLock.touch {}".format(_ms_string(self.owner))))
        if not answer.startswith("OK"):
            raise RuntimeError("Max 锁所有权丢失：" + answer)

    @staticmethod
    def _source(path):
        if not path or not Path(path).is_absolute() or not Path(path).is_file():
            raise ValueError("输入必须是存在的绝对路径文件：{}".format(path))
        return str(path)

    @staticmethod
    def _output(path, suffix):
        if not path or not Path(path).is_absolute() or Path(path).suffix.casefold() != suffix:
            raise ValueError("输出必须是后缀为 {} 的绝对路径：{}".format(suffix, path))
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 移走旧结果，防止将遗留文件误判为本次成功。
        core.backup_existing(path, move=True)
        return str(path)

    def _load(self, path):
        if rt.loadMaxFile(self._source(path), quiet=True,
                          missingExtFilesAction=rt.Name("logmsg"),
                          missingDLLsAction=rt.Name("abort"),
                          missingXRefsAction=rt.Name("abort")) is not True:
            raise RuntimeError("loadMaxFile 未成功：{}".format(path))

    @staticmethod
    def _mm_per_unit():
        # decodeValue 返回“1mm 等于多少系统单位”，取倒数；不更改系统单位。
        return core.unit_scale(1.0 / float(rt.units.decodeValue("1mm")))

    @staticmethod
    def _handle(node):
        return int(rt.getHandleByAnim(node))

    def _geometry(self, nodes, *, visible_only):
        result = []
        for node in nodes:
            if not rt.isValidNode(node):
                continue
            if rt.superClassOf(node) != rt.GeometryClass or rt.classOf(node) == rt.TargetObject:
                continue
            if rt.isGroupHead(node):
                continue
            if visible_only and bool(node.isHiddenInVpt):
                continue
            # 不能三角化的代理/程序几何不能悄悄从 PF1 包围盒中遗漏。
            mesh = rt.snapshotAsMesh(node)
            if mesh is None:
                raise RuntimeError("无法读取网格：{}".format(node.name))
            if int(rt.getNumFaces(mesh)) <= 0:
                continue
            result.append(node)
        return result

    @staticmethod
    def _bbox(nodes):
        boxes = []
        for node in nodes:
            bounds = rt.nodeGetBoundingBox(node, rt.Matrix3(1))
            boxes.append((tuple(bounds[0]), tuple(bounds[1])))
        return core.union_bbox(boxes)

    def _material_log(self, nodes):
        visited = set()

        def visit(material):
            if material is None:
                return
            identity = int(rt.getHandleByAnim(material))
            if identity in visited:
                return
            visited.add(identity)
            name = str(material.name)
            class_name = str(rt.classOf(material))
            kinds = core.material_kinds(name, class_name)
            if kinds:
                self.log("检测到{}材质：{}（{}），仅记录".format("、".join(kinds), name, class_name))
            for index in range(1, int(rt.getNumSubMtls(material)) + 1):
                visit(rt.getSubMtl(material, index))

        for node in nodes:
            try:
                visit(node.material)
            except Exception:
                self.log("材质检测失败（继续任务）：" + traceback.format_exc())

    def export_preview(self):
        self._load(self.job["max_file"])
        nodes = []
        for n in self._geometry(list(rt.objects), visible_only=True):
            if self._is_staging(n):
                self.log("导出预览时排除布景对象：{}".format(n.name))
            else:
                nodes.append(n)
        if not nodes:
            raise RuntimeError("排除白底模板自带对象后，模型里没有可见网格")
        bounds = self._bbox(nodes)
        document = core.bbox_document(*bounds, len(nodes), self._mm_per_unit())
        self._material_log(nodes)
        out_fbx = self._output(self.job["out_fbx"], ".fbx")
        out_bbox = self.job["out_bbox"]
        if not Path(out_bbox).is_absolute() or Path(out_bbox).suffix.casefold() != ".json":
            raise ValueError("out_bbox 必须是绝对路径 JSON 文件")
        rt.pluginManager.loadClass(rt.FBXEXP)
        # 备份并恢复导出器全局设置；拒绝未知参数，不默默导出错误坐标/单位。
        settings = [("ASCII", False), ("UpAxis", "Y"), ("ConvertUnit", "mm"),
                    ("ScaleFactor", 1.0), ("AxisConversionMethod", "Animation"),
                    ("Cameras", False), ("Lights", False), ("Animation", False),
                    ("EmbedTextures", False), ("ShowWarnings", False)]
        previous = []
        try:
            for key, value in settings:
                old = rt.FBXExporterGetParam(key)
                if str(old).casefold() == "unsupplied":
                    raise RuntimeError("FBX 导出器无法读取参数：" + key)
                previous.append((key, old))
                answer = rt.FBXExporterSetParam(key, value)
                # 实测 Max 2025：设置成功返回 True（不是 "OK"）
                if answer is not True and str(answer).casefold() not in ("ok", "true"):
                    raise RuntimeError("FBX 导出器拒绝参数 {}：{}".format(key, answer))
            rt.select(rt.Array(*nodes))
            self._touch()
            if rt.exportFile(out_fbx, rt.Name("noPrompt"), selectedOnly=True, using=rt.FBXEXP) is False:
                raise RuntimeError("FBX 导出失败")
            if not Path(out_fbx).is_file() or Path(out_fbx).stat().st_size == 0:
                raise RuntimeError("FBX 导出器未生成文件")
            core.atomic_write_json(out_bbox, document)
            self.log("预览已导出，网格数：{}".format(len(nodes)))
        finally:
            for key, old in reversed(previous):
                try:
                    rt.FBXExporterSetParam(key, old)
                except Exception:
                    self.log("恢复 FBX 参数 {} 失败：{}".format(key, traceback.format_exc()))

    def _remove_template_cameras(self):
        for node in list(rt.cameras):
            # rt.cameras 里也有相机目标点；删相机时目标点会一起被删，后面要跳过
            if not rt.isValidNode(node):
                continue
            if rt.superClassOf(node) == rt.Camera:
                target = node.target if rt.hasProperty(node, "target") else None
                rt.delete(node)
                if target is not None and rt.isValidNode(target):
                    rt.delete(target)

    def _template_names(self):
        """白底模板里的对象名。产品 .max 常常已经放在模板里（带模板的地面、墙、灯光、相机），
        这些对象不算产品：导出预览时排除，合并进模板时按重名跳过。只读文件头，不载入场景。"""
        cached = getattr(self, "_template_names_cache", None)
        if cached is not None:
            return cached
        template = self.job.get("template_max") or self.config.get("template_max")
        names = set()
        try:
            if template and Path(template).is_file():
                names = {str(n) for n in rt.getMAXFileObjectNames(template, quiet=True)}
        except Exception:
            self.log("读取模板对象名失败（继续任务）：" + traceback.format_exc())
        self._template_names_cache = names
        return names

    STAGING_MAX_SIZE_MM = 6000.0

    def _is_staging(self, node):
        """布景对象（不是产品）：模板同名对象、Plane 平面、任一边超过 6 米的几何体。
        产品 .max 里常带着白底模板的地面/墙或额外的大背景板。"""
        try:
            if str(node.name) in self._template_names():
                return True
            if rt.classOf(node) == rt.Plane:
                return True
            if rt.superClassOf(node) == rt.GeometryClass:
                size = node.max - node.min
                limit = self.STAGING_MAX_SIZE_MM / self._mm_per_unit()
                if max(float(size.x), float(size.y), float(size.z)) > limit:
                    return True
        except Exception:
            return False
        return False

    def _drop_staging(self, nodes):
        kept = []
        for node in nodes:
            if rt.isValidNode(node) and self._is_staging(node):
                self.log("去掉布景对象（不算产品）：{}".format(node.name))
                rt.delete(node)
            elif rt.isValidNode(node):
                kept.append(node)
        return kept

    def _drop_lights_cameras(self, nodes):
        """产品文件自带的灯光和相机不进入白底渲染。"""
        kept = []
        for node in nodes:
            if rt.isValidNode(node) and rt.superClassOf(node) in (rt.Light, rt.Camera):
                self.log("删除产品文件自带的 {}：{}".format(rt.superClassOf(node), node.name))
                rt.delete(node)
            elif rt.isValidNode(node):
                kept.append(node)
        return kept

    def _merge(self, path):
        before = {self._handle(node) for node in rt.objects}
        # skipDups：产品文件里和模板同名的对象（模板地面、墙、灯光、相机）不再合并
        result = rt.mergeMAXFile(self._source(path), rt.Name("select"), rt.Name("skipDups"),
                                 rt.Name("useMergedMtlDups"), rt.Name("neverReparent"), quiet=True,
                                 missingExtFilesAction=rt.Name("logmsg"),
                                 missingDLLsAction=rt.Name("abort"),
                                 missingXRefsAction=rt.Name("abort"))
        if result is not True:
            raise RuntimeError("mergeMAXFile 未成功")
        # 不依赖选中状态，组成员、辅助父节点、隐藏对象都计入移动集合。
        merged = [node for node in rt.objects if self._handle(node) not in before]
        return self._drop_staging(self._drop_lights_cameras(merged))

    def _keep_variant_nodes(self, merged):
        nodes = {self._handle(node): node for node in merged if rt.isValidNode(node)}
        parents, geometry, auxiliaries = {}, {}, set()
        for handle, node in nodes.items():
            parent = node.parent
            parents[handle] = self._handle(parent) if parent is not None else None
            superclass = rt.superClassOf(node)
            if rt.isGroupHead(node) or superclass == rt.Helper:
                auxiliaries.add(handle)
            elif superclass == rt.GeometryClass and rt.classOf(node) != rt.TargetObject:
                geometry[handle] = str(node.name)

        kept, removed = core.variant_node_selection(geometry, parents, auxiliaries, self.job["keep_nodes"])
        meshes = self._geometry([node for handle, node in nodes.items() if handle in kept], visible_only=True)
        if not meshes:
            raise core.VariantNodesNotFound()
        remaining = []
        for handle, node in nodes.items():
            if handle in removed:
                continue
            if parents[handle] in removed:
                transform = node.transform
                node.parent = None
                node.transform = transform
            remaining.append(node)
        for handle, node in nodes.items():
            if handle in removed and rt.isValidNode(node):
                rt.delete(node)
        self.log("只保留柜子 {}：保留 {} 个、删除 {} 个".format(
            self.job.get("variant_name", ""), len(kept), len(removed)))
        return remaining, meshes

    def _move_roots(self, merged, delta):
        handles = {self._handle(node) for node in merged}
        for node in merged:
            if node.parent is None or self._handle(node.parent) not in handles:
                transform = node.transform
                transform.row4 = transform.row4 + rt.Point3(*delta)
                node.transform = transform

    def _property(self, obj, candidates):
        return core.pick_property(candidates, lambda name: bool(rt.hasProperty(obj, name)))

    def _unsupported(self, message):
        if not hasattr(self, "_calibration_unsupported"):
            self._calibration_unsupported = []
        if message not in self._calibration_unsupported:
            self._calibration_unsupported.append(message)
            self.log(message)

    def _material_data(self, material):
        name, class_name = str(material.name), str(rt.classOf(material))
        target, seen = material, set()
        while target is not None and self._material_family(target) == "ray_switch":
            handle = self._handle(target)
            if handle in seen:
                target = None
                break
            seen.add(handle)
            children = self._ray_children(target)
            target = children[0] if children else None
        available = lambda prop: target is not None and bool(rt.hasProperty(target, prop))
        color_prop = core.material_property(available, "color")
        map_prop = core.material_property(available, "map")
        color = rt.getProperty(target, color_prop) if color_prop else None
        rgb = [float(getattr(color, channel)) for channel in ("r", "g", "b")] if color is not None else None
        texmap = rt.getProperty(target, map_prop) if map_prop else None
        params = {}
        for prop in core.material_parameters(available):
            current = rt.getProperty(target, prop)
            if all(hasattr(current, c) for c in ("r", "g", "b")):
                current = [float(getattr(current, c)) for c in ("r", "g", "b")]
            else:
                current = float(current)
            params[prop] = current
        bitmap = self._bitmap_name(texmap)
        kind = core.material_kind(name + (" " + str(target.name) if target is not None else ""),
                                  str(rt.classOf(target)) if target is not None else class_name,
                                  bool(bitmap), texmap is None and rgb is not None)
        return {"name": name, "class": class_name, "color": rgb, "map": texmap,
                "color_prop": color_prop, "map_prop": map_prop,
                "bitmap": bitmap, "params": params, "kind": kind,
                "family": core.material_family(available)}

    def _material_family(self, material):
        return core.material_family(lambda prop: bool(rt.hasProperty(material, prop)))

    def _ray_children(self, material):
        return core.ray_switch_children(material, rt.getPropNames(material), rt.getProperty, self._handle)

    def _multi_main(self, node, material):
        try:
            submaterials = list(rt.getProperty(material, "materialList"))
            ids = (list(rt.getProperty(material, "materialIDList")) if rt.hasProperty(material, "materialIDList")
                   else list(range(1, len(submaterials) + 1)))
            mesh = rt.snapshotAsMesh(node)
            main = core.dominant_submaterial(submaterials, ids,
                    (rt.getFaceMatID(mesh, index) for index in range(1, int(rt.getNumFaces(mesh)) + 1)))
            self._unsupported("{}：多维材质分区按面数最多的材质 ID 记，不能区分各面材质".format(node.name))
            return main
        except Exception as exc:
            self._unsupported("{}：无法读取多维材质面 ID，分区按未分配材质处理：{}".format(node.name, exc))
            return None

    def _bitmap_name(self, texmap, visited=None):
        if texmap is None:
            return None
        visited = set() if visited is None else visited
        handle = self._handle(texmap)
        if handle in visited:
            return None
        visited.add(handle)
        for prop in ("filename", "fileName"):
            if rt.hasProperty(texmap, prop):
                filename = str(rt.getProperty(texmap, prop) or "")
                if filename:
                    return filename.replace("\\", "/").rsplit("/", 1)[-1]
        for index in range(1, int(rt.getNumSubTexmaps(texmap)) + 1):
            result = self._bitmap_name(rt.getSubTexmap(texmap, index), visited)
            if result:
                return result
        return None

    def _product_materials(self, meshes):
        """递归收集且按 handle 去重；同一物体引用多次的子材质只计一次。"""
        materials, mains = {}, []
        for node in meshes:
            visited = set()
            def visit(material):
                if material is None:
                    return
                handle = self._handle(material)
                if handle in visited:
                    return
                visited.add(handle)
                if self._material_family(material) == "multi":
                    for child in rt.getProperty(material, "materialList"):
                        visit(child)
                    return
                if handle not in materials:
                    materials[handle] = {"material": material, "objects": 0}
                materials[handle]["objects"] += 1
            material = node.material
            visit(material)
            # 光线切换保留外层名称；只有多维材质按面数选槽。
            seen = set()
            while material is not None and self._material_family(material) == "multi":
                handle = self._handle(material)
                if handle in seen:
                    self._unsupported("{}：子材质循环引用，分区按未分配材质处理".format(node.name))
                    material = None
                    break
                seen.add(handle)
                material = self._multi_main(node, material)
            mains.append((node, material))
        return materials, mains

    def _color_correct_class(self):
        """中文版 Max 里「Corona色彩校正」没有英文全局名（rt.CoronaColorCorrect 不存在），
        从 textureMap.classes 里按名字找；找到后缓存。"""
        cached = getattr(self, "_color_correct_class_cache", None)
        if cached is not None:
            return cached
        found = getattr(rt, "CoronaColorCorrect", None)
        if found is None:
            for cls in rt.textureMap.classes:
                name = str(cls)
                if name in core.COLOR_CORRECT_CLASS_NAMES or (
                        "Corona" in name and ("ColorCorrect" in name or "色彩校正" in name)):
                    found = cls
                    break
        self._color_correct_class_cache = found
        return found

    def _correct_map(self, original, values, label):
        """只创建一层外包；原贴图（包括原有校正层）完整保留。"""
        sources = getattr(self, "_correction_sources", None)
        if sources is None:
            sources = self._correction_sources = {}
        # 同一任务重复套用时替换本执行器自己的层，不叠加第二层。
        if self._handle(original) in sources:
            original = sources[self._handle(original)]
        correction_class = self._color_correct_class()
        if correction_class is None:
            self._unsupported("{}：找不到 Corona 色彩校正贴图类".format(label))
            return original
        correction = correction_class()
        prop = self._property(correction, core.COLOR_CORRECT_PROPERTIES["input"])
        if prop is None:
            self._unsupported("{}：没有可调的 色彩校正输入".format(label))
            return original
        rt.setProperty(correction, prop, original)
        sources[self._handle(correction)] = original
        for key, value in values.items():
            if key not in core.COLOR_CORRECT_PROPERTIES or key == "input":
                continue
            neutral = 0 if key == "hue_shift_deg" else 1
            if value == neutral:
                continue
            prop = self._property(correction, core.COLOR_CORRECT_PROPERTIES[key])
            if prop is None:
                self._unsupported("{}：没有可调的 {}".format(label, key))
                continue
            try:
                default = float(rt.getProperty(correction, prop))
                rt.setProperty(correction, prop, core.color_correct_value(key, value, default, prop))
            except Exception as exc:
                self._unsupported("{}：色彩校正 {} 未套用：{}".format(label, key, exc))
        return correction

    def _apply_material(self, material, values, name=None, kind=None, visited=None):
        visited = set() if visited is None else visited
        handle = self._handle(material)
        if handle in visited:
            return
        visited.add(handle)
        data = self._material_data(material)
        name = data["name"] if name is None else name
        kind = data["kind"] if kind is None else kind
        if self._material_family(material) == "ray_switch":
            for child in self._ray_children(material):
                try:
                    self._apply_material(child, values, name, kind, visited)
                except Exception as exc:
                    self._unsupported("{}：子材质 {} 调整失败：{}".format(name, child.name, exc))
            return
        color_values = {key: value for key, value in values.items()
                        if key in ("hue_shift_deg", "saturation_mul", "brightness_mul", "contrast_mul")
                        and value != (0 if key == "hue_shift_deg" else 1)}
        if color_values:
            if data["map"] is not None:
                corrected = self._correct_map(data["map"], color_values, name)
                rt.setProperty(material, data["map_prop"], corrected)
            elif data["color"] is not None:
                rgb = core.adjust_hsv(data["color"], **color_values)
                rt.setProperty(material, data["color_prop"], rt.Color(*rgb))
            else:
                self._unsupported("{}：没有可调的 漫反射/基础色".format(name))
        for key in ("reflection_mul", "glossiness_delta", "refraction_mul", "ior", "opacity_mul"):
            value = values.get(key)
            if value is None or (key != "ior" and value == (0 if key == "glossiness_delta" else 1)):
                continue
            if key in ("refraction_mul", "ior", "opacity_mul") and kind != "glass":
                self._unsupported("{}：{} 只对 glass 套用，已跳过".format(name, key))
                continue
            prop = core.material_property(lambda p: bool(rt.hasProperty(material, p)), key)
            if prop is None:
                self._unsupported("{}：没有可调的 {}".format(name, key))
                continue
            try:
                gloss_mode = False
                if key == "glossiness_delta" and self._material_family(material) == "physical":
                    mode = self._property(material, ("roughnessMode", "baseRoughnessMode"))
                    gloss_mode = mode is not None and int(rt.getProperty(material, mode)) == 0
                current = float(rt.getProperty(material, prop))
                rt.setProperty(material, prop, core.adjusted_scalar(current, key, value, prop, glossiness_mode=gloss_mode))
            except Exception as exc:
                self._unsupported("{}：调整 {} 失败：{}".format(name, key, exc))

    def _light_nodes(self, lights):
        nodes = {self._handle(node): node for node in lights}
        for light in lights:
            target = light.target if rt.hasProperty(light, "target") else None
            if target is not None and rt.isValidNode(target):
                nodes[self._handle(target)] = target
        return list(nodes.values())

    def _light_transforms(self, lights):
        # 先拍下所有世界矩阵，再按父子顺序赋值，避免父子灯重复旋转。
        nodes = self._light_nodes(lights)
        def depth(node):
            count, visited = 0, set()
            parent = node.parent
            while parent is not None and self._handle(parent) not in visited:
                visited.add(self._handle(parent))
                count += 1
                parent = parent.parent
            return count
        return [(node, rt.copy(node.transform)) for node in sorted(nodes, key=depth)]

    @staticmethod
    def _rotate_transforms(snapshots, center, angle):
        for node, transform in snapshots:
            rows = [tuple(getattr(transform, "row{}".format(i))) for i in range(1, 5)]
            rotated = core.rotate_transform_z(rows, center, angle)
            node.transform = rt.Matrix3(*(rt.Point3(*row) for row in rotated))

    @contextmanager
    def _item_lights(self, item, bounds=None, scale=1):
        lighting = core.normalize_lighting(item.get("lighting"))
        if lighting is not None and lighting["azimuth_deg"] is not None:
            with self._lighting(item, lighting, bounds, scale):
                yield
            return
        snapshots = []
        intensities = []
        overrides = getattr(self, "_overrides", None)
        try:
            if overrides is not None:
                for light in self._template_lights:
                    factor = overrides["lights"]["intensity_mul"] * overrides["lights"]["per_light"].get(
                        str(getattr(light, "name", "")), {}).get("intensity_mul", 1)
                    if factor != 1:
                        self._scale_light(light, factor, intensities)
                angle = core.item_light_angle(overrides, item["no"])
                if angle:
                    snapshots = self._light_transforms(self._template_lights)
                    self._rotate_transforms(snapshots, self._light_center, angle)
            self._record_lighting(item, None)
            yield
        finally:
            errors = []
            for light, prop, value in reversed(intensities):
                try:
                    rt.setProperty(light, prop, value)
                except Exception as exc:
                    errors.append(str(exc))
            for node, transform in snapshots:
                try:
                    node.transform = transform
                except Exception as exc:
                    errors.append(str(exc))
            if errors:
                raise SceneRestoreError("恢复逐项灯光失败：" + "；".join(errors))

    def _scale_light(self, light, factor, snapshots):
        prop = self._property(light, core.LIGHT_INTENSITY_PROPERTIES)
        if prop is None:
            self._unsupported("{}：没有可调的灯光强度，已跳过该灯".format(light.name))
            return
        try:
            value = rt.getProperty(light, prop)
            adjusted = float(value) * factor
            if not math.isfinite(adjusted):
                raise ValueError("灯光强度不是有限数值")
        except Exception as exc:
            self._unsupported("{}：无法读取灯光强度，已跳过该灯：{}".format(light.name, exc))
            return
        snapshots.append((light, prop, value))
        rt.setProperty(light, prop, adjusted)

    def _corona_light_class(self):
        """中文 Max 不能依赖 CoronaLight 英文全局符号。"""
        try:
            classes = list(rt.light.classes)
            for candidate in core.CORONA_LIGHT_CLASS_NAMES:
                for cls in classes:
                    if str(cls).casefold() == candidate.casefold():
                        return cls
            for cls in classes:
                if core.is_corona_light_class_name(str(cls)):
                    return cls
        except Exception as exc:
            self._unsupported("打光：无法枚举 rt.light.classes：{}".format(exc))
        self._unsupported("打光：找不到 Corona 灯类（CoronaLight、Corona灯光、CR灯光），已跳过打光，继续渲染")
        return None

    def _set_key_property(self, light, prop, value):
        if not rt.hasProperty(light, prop):
            raise RuntimeError("主光缺少属性 {}".format(prop))
        rt.setProperty(light, prop, value)

    def _record_lighting(self, item, applied):
        manifest = getattr(self, "_scene_manifest", None)
        if manifest is not None:
            manifest.setdefault("lighting_applied", {})[str(item["no"])] = applied
            core.atomic_write_json(self._calibration_dir() / "scene_manifest.json", manifest)

    def _lighting_tuning(self):
        return core.normalize_lighting_tuning(self.job.get("lighting_tuning"))

    @staticmethod
    def _light_axis(light):
        origin = core.vector3(tuple(light.pos))
        try:
            target = light.target
        except Exception:
            target = None
        if target is not None and rt.isValidNode(target):
            direction = tuple(b - a for a, b in zip(origin, core.vector3(tuple(target.pos))))
        elif "omni" in str(rt.classOf(light)).casefold():
            raise ValueError("全向灯没有主轴")
        else:
            direction = tuple(-value for value in core.vector3(tuple(light.transform.row3)))
        length = math.hypot(*direction)
        if not length:
            raise ValueError("灯光主轴长度为零")
        return origin, tuple(value / length for value in direction)

    def _lighting_background(self, nodes):
        found = {}
        for node in nodes:
            name = str(node.name)
            if name in core.LIGHTING_BACKGROUND_NAMES:
                try:
                    found[name] = self._bbox([node])
                except Exception as exc:
                    self.log_file("打光：读取 {} 包围盒失败：{}".format(name, exc))
        return found

    def _is_background_fill(self, light, product):
        try:
            origin, direction = self._light_axis(light)
            hit = core.lighting_ray_box_distance(origin, direction, product)
            matches = [(core.lighting_ray_box_distance(origin, direction, box), name)
                       for name, box in getattr(self, "_lighting_background_boxes", {}).items()]
            distance, name = min(matches, default=(math.inf, ""))
        except Exception as exc:
            self.log("背景补光：{} 按普通补光处理，无法确定灯光主轴：{}".format(light.name, exc))
            return False
        if distance < hit:
            self.log("背景补光：{} 主轴先到达 {}，采用背景补光比例".format(light.name, name))
            return True
        self.log("背景补光：{} 主轴未先命中墙或地面，采用普通补光比例".format(light.name))
        return False

    @staticmethod
    def _probe_properties(obj, tokens, prefixes=()):
        names = sorted(rt.getPropNames(obj), key=lambda prop: str(prop).casefold())
        properties = {}
        for prop in names:
            name = str(prop).lstrip("#")
            lower = name.casefold()
            if lower.startswith(prefixes) or any(token in lower for token in tokens):
                try:
                    properties[name] = str(rt.getProperty(obj, prop))
                except Exception as exc:
                    properties[name] = "读取失败：{}".format(exc)
        return properties

    def _probe_lighting(self):
        self.log_file("打光探测：作业 {}".format(self.job["job_id"]))
        try:
            renderer = rt.renderers.current
            self.log_file("打光探测：渲染器类 {}".format(rt.classOf(renderer)))
            props = self._probe_properties(
                renderer, ("env", "ambient", "background", "global", "illum", "expos",
                           "tone", "颜色", "背景", "环境", "照明", "曝光", "gi_", "_gi",
                           "skylight", "sunlight", "secondary", "primary", "whitebal",
                           "colormapping", "photographic", "pathtrac", "uhdcache"),
                ("bg", "gi"))
            for name, value in props.items():
                self.log_file("打光探测：渲染器 {}={}".format(name, value))
            self.log_file("打光探测：场景 useEnvironmentMap={}，environmentMap={}，backgroundColor={}".format(
                rt.useEnvironmentMap, rt.environmentMap, rt.backgroundColor))
        except Exception as exc:
            self.log_file("打光探测：读取渲染器或场景环境失败：{}".format(exc))
        for light in self._template_lights:
            name = str(light.name)
            try:
                self.log_file("打光探测：模板灯 {}，类 {}，世界位置 {}".format(
                    name, rt.classOf(light), tuple(light.pos)))
                props = self._probe_properties(light, ("intens", "mult", "power", "lumen",
                                                       "width", "length", "height", "size",
                                                       "radius", "diameter", "shape", "target",
                                                       "rotation", "direction", "area", "unit"))
                self.log_file("打光探测：模板灯 {} 强度和尺寸属性 {}".format(name, props))
            except Exception as exc:
                self.log_file("打光探测：读取模板灯 {} 属性失败：{}".format(name, exc))
            try:
                self.log_file("打光探测：模板灯 {} 世界朝向 {}".format(name, self._light_axis(light)[1]))
            except Exception as exc:
                self.log_file("打光探测：模板灯 {} 朝向不可用：{}".format(name, exc))
        for name in core.LIGHTING_BACKGROUND_NAMES:
            self.log_file("打光探测：{} 世界包围盒 {}".format(
                name, self._lighting_background_boxes.get(name, "未找到或不可读")))

    @staticmethod
    def _restore_environment_gi(snapshots):
        errors = []
        for renderer, prop, original in reversed(snapshots):
            try:
                rt.setProperty(renderer, prop, original)
                current = rt.getProperty(renderer, prop)
                # 颜色等 MAXScript 值按字面比较，避免包装对象按身份比较误报
                if current != original and str(current) != str(original):
                    raise ValueError("回读值与原值不同")
            except Exception as exc:
                errors.append("恢复 Corona 环境属性 {}：{}".format(prop, exc))
        snapshots.clear()
        return errors

    @staticmethod
    def _direct_background_source(bindings, read):
        source_map, source_color = bindings.get("source_map"), bindings.get("source_color")
        if source_map and source_color:
            raise ValueError("原背景只可绑定一种来源")
        if source_map or (not source_color and rt.useEnvironmentMap):
            source = read(source_map) if source_map else rt.environmentMap
            if source is None:
                raise ValueError("场景环境贴图为空，无法保留直接可见背景")
            return bindings.get("direct_map"), source
        source = read(source_color) if source_color else rt.backgroundColor
        if source is None:
            raise ValueError("场景背景色为空")
        if bindings.get("direct_color"):
            return bindings["direct_color"], source
        name = bindings.get("solid_map_class")
        color_prop = bindings.get("solid_map_color")
        if not name or not color_prop or not bindings.get("direct_map"):
            raise ValueError("没有配置可保留纯色背景的颜色属性或纯色贴图")
        cls = next((cls for cls in rt.textureMap.classes if str(cls).casefold() == name.casefold()), None)
        if cls is None:
            raise ValueError("找不到纯色贴图类 {}".format(name))
        color_map = cls()
        if not rt.hasProperty(color_map, color_prop):
            raise ValueError("纯色贴图缺少属性 {}".format(color_prop))
        rt.setProperty(color_map, color_prop, source)
        if rt.getProperty(color_map, color_prop) != source:
            raise ValueError("纯色贴图没有保留场景背景色")
        return bindings["direct_map"], color_map

    def _apply_environment_gi(self, bindings, ratio, snapshots):
        if ratio == 1:
            return 1.0
        try:
            renderer = rt.renderers.current
            if "corona" not in str(rt.classOf(renderer)).casefold():
                raise ValueError("当前渲染器不是 Corona")
            gi, enabled = (bindings.get(key) for key in ("gi_multiplier", "direct_enabled"))
            props = [bindings.get(key) for key in (
                "gi_multiplier", "direct_enabled", "direct_map", "direct_color", "source_map", "source_color")]
            names = [prop.casefold() for prop in props if prop]
            source_color_prop = bindings.get("source_color")
            if not (gi or source_color_prop) or not enabled or len(names) != len(set(names)):
                raise ValueError("没有配置互相独立的 GI 倍率（或环境色）与直接可见背景属性，请先探测")

            def read(prop):
                if not prop or not rt.hasProperty(renderer, prop):
                    raise ValueError("缺少 Corona 环境属性 {}".format(prop or "（未配置）"))
                return rt.getProperty(renderer, prop)

            def change(prop, old, new):
                snapshots.append((renderer, prop, old))
                rt.setProperty(renderer, prop, new)

            original_gi = read(gi) if gi else None
            direct_on = read(enabled)
            if (gi and not core.lighting_number(original_gi)) or type(direct_on) is not bool:
                raise ValueError("GI 倍率或直接可见背景开关的属性值类型不符")
            if not direct_on:
                prop, source = self._direct_background_source(bindings, read)
                change(prop, read(prop), source)
                change(enabled, direct_on, True)
                if read(enabled) is not True or (read(prop) != source and str(read(prop)) != str(source)):
                    raise ValueError("直接可见背景覆盖未保留场景背景值")
            if not gi:
                # 2026-11-21 中文 Max 2025 + Corona 12 实测：Corona 没有环境 GI 倍率属性；
                # 调暗 BG_COLOR 能压低白色环境对产品的均匀照明（方盒背光面最暗处 60→35），
                # 改 bg_colorList 不起作用。画面里看到的背景已由上面的直接可见覆盖保持原色。
                original_color = read(source_color_prop)
                scaled = rt.Color(original_color.r * ratio, original_color.g * ratio, original_color.b * ratio)
                change(source_color_prop, original_color, scaled)
                self.log("Corona 场景环境色 {} 乘以 {:g}，直接可见背景已单独保留".format(source_color_prop, ratio))
                return ratio
            adjusted = original_gi * ratio
            change(gi, original_gi, adjusted)
            if not math.isclose(read(gi), adjusted, rel_tol=1e-6, abs_tol=1e-9):
                raise ValueError("Corona 未采用指定的 GI 倍率")
            self.log("Corona 场景环境 GI 倍率乘以 {:g}，直接可见背景已单独保留".format(ratio))
            return ratio
        except Exception as exc:
            errors = self._restore_environment_gi(snapshots)
            if errors:
                raise SceneRestoreError("；".join(errors)) from exc
            self._unsupported("打光：已跳过 Corona 环境 GI 调整：{}".format(exc))
            return 1.0

    @contextmanager
    def _lighting(self, item, lighting, bounds, scale):
        key = target = None
        intensities = []
        environment = []
        applied = None

        def restore():
            nonlocal key, target
            errors = self._restore_environment_gi(environment)
            for light, prop, value in reversed(intensities):
                try:
                    rt.setProperty(light, prop, value)
                except Exception as exc:
                    errors.append("恢复模板灯 {}：{}".format(light.name, exc))
            intensities.clear()
            # targeted=True 可能自动建立目标，即使后续设置失败也要回收。
            if key is not None and target is None:
                try:
                    if rt.hasProperty(key, "target"):
                        target = rt.getProperty(key, "target")
                except Exception as exc:
                    errors.append("读取临时主光目标：{}".format(exc))
            for node in (key, target):
                try:
                    if node is not None and rt.isValidNode(node):
                        rt.delete(node)
                        if rt.isValidNode(node):
                            raise RuntimeError("删除后节点仍有效")
                except Exception as exc:
                    errors.append("删除临时灯光节点：{}".format(exc))
            key = target = None
            if errors:
                raise SceneRestoreError("恢复打光失败：" + "；".join(errors))

        try:
            try:
                cls = self._corona_light_class()
                if cls is None:
                    raise RuntimeError("未找到可用的 Corona 灯类")
                low, high = core.validate_bbox(*bounds)
                diagonal = math.dist(low, high)
                center = core.bbox_center(low, high)
                tuning = self._lighting_tuning()
                base = self.job.get("key_light_intensity",
                                    self.config.get("key_light_intensity", core.LIGHTING_DEFAULT_KEY_INTENSITY))
                overrides = getattr(self, "_overrides", None)
                ai_mul = core.clamp_number(overrides["lights"]["intensity_mul"], 0.85, 1.15) if overrides else 1.0
                response = core.lighting_response(lighting, tuning, base, ai_mul)
                distance = response["key_distance"] * diagonal
                nominal_distance = distance
                room = getattr(self, "_template_room", None)
                if room is not None:
                    direction = core.lighting_position_max(lighting["azimuth_deg"], lighting["elevation_deg"],
                                                           1.0, (0.0, 0.0, 0.0))
                    # 离每面墙至少 30 厘米再加半块灯板，灯板边缘也不会碰到布景
                    margin = core.mm_to_units(300, scale) + 0.5 * response["key_size"] * diagonal
                    clamped = core.clamp_key_distance(distance, center, direction, room, margin)
                    if clamped < distance:
                        self.log("主光距离收进模板布景内：{:.0f} → {:.0f} 毫米".format(
                            core.units_to_mm(distance, scale), core.units_to_mm(clamped, scale)))
                        distance = clamped
                size = response["key_size"] * diagonal
                position = core.lighting_position_max(lighting["azimuth_deg"], lighting["elevation_deg"],
                                                      distance, center)
                strength, fill = response["key_strength"], response["fill_ratio"]
                # 灯板大小不变时照度按距离平方变化：收近的主光按比例减弱，各方向曝光一致
                strength *= core.key_distance_falloff(distance, nominal_distance)
                key = cls()
                key.name = "白底临时主光_{}".format(item["no"])
                for prop, value in (("targeted", True), ("shape", 0), ("width", size),
                                    ("height", size), ("visibleDirectly", False),
                                    ("visibleRefl", False), ("visibleRefract", False), ("on", True),
                                    ("intensity", strength)):
                    # 实测「CR灯光」没有 visibleRefract：可选属性不存在就跳过，主光照常打
                    if prop == "visibleRefract" and not rt.hasProperty(key, prop):
                        self.log("主光没有 {} 属性，跳过这一项".format(prop))
                        continue
                    self._set_key_property(key, prop, value)
                # 实测「CR灯光」：hasProperty(key, "target") 为 false（target 是节点属性，不在参数块里），
                # 但 targeted=true 之后 key.target 能直接取到，所以直接取，取不到再补一个目标点。
                try:
                    target = key.target
                except Exception:
                    target = None
                if target is None or not rt.isValidNode(target):
                    target = rt.TargetObject()
                    key.target = target
                key.pos = rt.Point3(*position)
                target.pos = rt.Point3(*center)
                # 优先继承 Corona 模板灯，不把其他种类灯的颜色枚举套给 Corona。
                source = next((light for light in self._template_lights
                               if core.is_corona_light_class_name(str(rt.classOf(light)))), None)
                if source is None:
                    self._unsupported("打光：没有 Corona 模板灯可复制颜色，主光采用插件默认颜色；需实测中性白")
                else:
                    for prop in ("colorMode", "blackbodyTemp", "color"):
                        if rt.hasProperty(source, prop) and rt.hasProperty(key, prop):
                            rt.setProperty(key, prop, rt.getProperty(source, prop))
                        else:
                            self._unsupported("打光：模板灯或主光缺少颜色属性 {}，该属性未复制".format(prop))
                bg_fill = response["bg_fill_ratio"]
                if bg_fill != fill:
                    self.log("背景补光判定：沿模板灯目标方向（无目标时用局部 -Z），轴线先命中墙或地面包围盒才保留补光")
                for light in self._template_lights:
                    ratio = bg_fill if bg_fill != fill and self._is_background_fill(light, (low, high)) else fill
                    self._scale_light(light, ratio * lighting["intensity"] * ai_mul, intensities)
                env_gi = self._apply_environment_gi(tuning.get("env_gi_properties", {}),
                                                      response["env_gi"], environment)
                applied = {"applied": True, "lighting": lighting,
                           "pos_mm": [core.units_to_mm(v, scale) for v in key.pos],
                           "intensity": float(rt.getProperty(key, "intensity")), "intensity_mul": ai_mul,
                           "fill_ratio": fill, "bg_fill_ratio": bg_fill, "env_gi": env_gi,
                           "key_size_mm": core.units_to_mm(size, scale),
                           "key_distance_mm": core.units_to_mm(distance, scale),
                           "description": core.lighting_description(lighting)}
                self.log("明细 {} 已打光：{}；主光强度 {:g}；补光比例 {:g}".format(
                    item["no"], applied["description"], strength, fill))
            except SceneRestoreError:
                raise
            except Exception as exc:
                restore()
                message = "明细 {} 跳过打光，继续渲染：{}".format(item["no"], exc)
                self._unsupported(message)
                applied = {"applied": False, "reason": message}
            self._record_lighting(item, applied)
            yield
        finally:
            restore()

    def _reflect_map(self):
        renderer = rt.renderers.current
        prop = self._property(renderer, core.REFLECT_MAP_PROPERTIES)
        return renderer, prop, rt.getProperty(renderer, prop) if prop else None

    def _apply_calibration(self, meshes, lights, center, scale):
        self._calibration_unsupported = []
        self._template_lights, self._light_center = lights, center
        self._overrides = core.clamp_overrides(self.job.get("overrides"))
        materials, mains = self._product_materials(meshes)
        adjusted = set()
        for entry in materials.values():
            material = entry["material"]
            values = self._overrides["materials"].get(str(material.name))
            if values:
                try:
                    self._apply_material(material, values, visited=adjusted)
                except Exception as exc:
                    self._unsupported("{}：套用材质调整失败：{}".format(material.name, exc))
        known = {str(entry["material"].name) for entry in materials.values()}
        for name in self._overrides["materials"].keys() - known:
            self._unsupported("{}：产品中没有此材质".format(name))
        # 灯光强度、方向统一在逐项上下文套用并恢复，避免污染其他项的 lighting。
        strength = self._overrides["environment"]["reflection_strength_mul"]
        if strength != 1:
            try:
                renderer, prop, texmap = self._reflect_map()
                if prop is None or texmap is None:
                    self._unsupported("环境：没有可调的 反射覆盖贴图")
                else:
                    rt.setProperty(renderer, prop, self._correct_map(texmap, {"brightness_mul": strength}, "环境反射"))
            except Exception as exc:
                self._unsupported("环境反射调整失败：{}".format(exc))
        if self.job.get("want_manifest"):
            self._write_calibration(materials, mains, lights, core.bbox_center(*self._bbox(meshes)), scale)

    def _calibration_dir(self):
        return core.calibration_directory(self.exchange / "tasks" / self.job["task_id"], self.job.get("calibration_round", 0))

    def _write_calibration(self, materials, mains, lights, center, scale):
        directory = self._calibration_dir()
        manifest = {"materials": [], "lights": [], "environment": {"reflect_map": None},
                    "product_center_mm": [core.units_to_mm(v, scale) for v in center],
                    "unsupported": self._calibration_unsupported}
        for entry in materials.values():
            material = entry["material"]
            try:
                data = self._material_data(material)
                if data["family"] is None:
                    self._unsupported("{}：不支持此材质类 {} 的属性调整".format(data["name"], data["class"]))
                data["objects"] = entry["objects"]
                for key in ("map", "map_prop", "color_prop", "family"):
                    del data[key]
                manifest["materials"].append(data)
            except Exception as exc:
                self._unsupported("{}：读取材质清单失败：{}".format(material.name, exc))
        for light in lights:
            try:
                prop = self._property(light, core.LIGHT_INTENSITY_PROPERTIES)
                manifest["lights"].append({"name": str(light.name),
                    "intensity": float(rt.getProperty(light, prop)) if prop else None,
                    "pos_mm": [core.units_to_mm(v, scale) for v in light.pos]})
                if prop is None:
                    self._unsupported("{}：无法读取灯光强度".format(light.name))
            except Exception as exc:
                self._unsupported("{}：读取灯光清单失败：{}".format(light.name, exc))
        try:
            _, _, texmap = self._reflect_map()
            manifest["environment"]["reflect_map"] = str(texmap.name) if texmap is not None else None
        except Exception as exc:
            self._unsupported("读取环境反射清单失败：{}".format(exc))
        identities = dict.fromkeys(self._handle(m) if m is not None else None for _, m in mains)
        colors = dict(zip(identities, core.region_palette(len(identities))))
        legend = {}
        for node, material in mains:
            handle = self._handle(material) if material is not None else None
            rgb = colors[handle]
            legend["#{:02X}{:02X}{:02X}".format(*rgb)] = str(material.name) if material is not None else "未分配材质"
            try:
                node.wirecolor = rt.Color(*rgb)
            except Exception as exc:
                self._unsupported("{}：设置材质分区线框色失败：{}".format(node.name, exc))
        if None in identities:
            self._unsupported("部分产品对象没有主材质，分区标为未分配材质")
        core.atomic_write_json(directory / "scene_manifest.json", manifest)
        self._scene_manifest = manifest
        core.atomic_write_json(directory / "regions" / "legend.json", legend)
        self.log("已输出 AI 校准场景清单与分区图例：{}".format(directory))

    @contextmanager
    def _smallshot_time(self):
        renderer = rt.renderers.current
        changed = None
        try:
            is_corona = "corona" in str(rt.classOf(renderer)).casefold()
            if is_corona:
                # 以下属性已由模板实测确认；只读取其余限制和反射覆盖，绝不写入。
                for name in ("progressive_passlimit", "adaptivity_targeterror", "bg_overrideReflect"):
                    try:
                        if rt.hasProperty(renderer, name):
                            self.log("沿用 Corona {}={}".format(name, rt.getProperty(renderer, name)))
                    except Exception:
                        self.log("读取 Corona 属性 {} 失败：{}".format(name, traceback.format_exc()))
            if self.job["mode"] == "smallshot":
                limit = self.job.get("time_limit_sec")
                if limit is None:
                    limit = self.config["smallshot_time_limit_sec"]
                milliseconds = core.smallshot_time_limit_ms(limit)
                if not is_corona:
                    self.log("当前渲染器不是 Corona，未更改时间上限")
                else:
                    name = "progressive_timelimit"
                    if rt.hasProperty(renderer, name):
                        changed = (name, rt.getProperty(renderer, name))
                        rt.setProperty(renderer, name, milliseconds)
                        self.log("临时设置 Corona {}={}，暂按毫秒（单位待实测）".format(name, milliseconds))
                    else:
                        self.log("找不到 Corona progressive_timelimit，沿用模板设置")
            yield
        finally:
            if changed is not None:
                rt.setProperty(renderer, changed[0], changed[1])
                self.log("已恢复 Corona 时间上限：{}={}".format(*changed))

    @contextmanager
    def _back_view_visibility(self, item, template_nodes, anchor, scale):
        previous = []
        try:
            azimuth = core.camera_azimuth_deg(item["camera"])
            if abs(azimuth) > 100:
                for node in template_nodes:
                    if not rt.isValidNode(node):
                        continue
                    bounds = self._bbox([node])
                    if core.should_hide_template_geometry(*bounds, anchor["back_y_mm"], azimuth, scale):
                        # isNodeHidden 只改节点自身标记，不清除原有图层隐藏状态。
                        previous.append((node, bool(node.isNodeHidden)))
                        node.isNodeHidden = True
                self.log("背面视角 {:.3f}°，临时隐藏 {} 个模板几何体".format(azimuth, len(previous)))
            yield
        finally:
            errors = []
            for node, was_hidden in reversed(previous):
                try:
                    if not rt.isValidNode(node):
                        raise RuntimeError("待恢复的模板节点已失效")
                    node.isNodeHidden = was_hidden
                except Exception:
                    errors.append(traceback.format_exc())
            if errors:
                raise SceneRestoreError("恢复模板隐藏状态失败：" + "\n".join(errors))

    def _mask_setup(self, item, restore_paths):
        manager = rt.maxOps.GetCurRenderElementMgr()
        active_indices = []
        elements = []
        for index in range(int(manager.NumRenderElements())):
            element = manager.GetRenderElement(index)
            enabled = not rt.hasProperty(element, "enabled") or bool(element.enabled)
            if enabled:
                active_indices.append(index)
            names = [str(rt.classOf(element))]
            for prop in ("elementName", "name"):
                if rt.hasProperty(element, prop):
                    names.append(str(rt.getProperty(element, prop)))
            elements.append({"index": index, "enabled": enabled, "names": names})
        selected = core.select_mask_elements(elements)
        routes = []
        messages = []
        if selected["id_index"] is None:
            messages.append("没有找到遮罩_ID，也没有可用的模糊匹配元素")
        if selected["wire_index"] is None:
            self.log("没有精确匹配到遮罩_线框颜色，未更改其他通道的输出路径")
        destinations = []
        no = item["no"]
        if isinstance(no, bool) or not isinstance(no, int) or no <= 0:
            raise ValueError("明细 no 必须为正整数")
        # 模板里渲染元素的原输出路径指向旧项目文件夹，存不进去时 Max 会弹"图像 I/O 错误"并卡住。
        # 所以每个启用的元素都必须改到本任务目录：小样放 smallshots/_extra，成品放 final/_extra。
        mode_dir = "final" if self.job.get("mode") == "final" else "smallshots"
        extra = self.exchange / "tasks" / self.job["task_id"] / mode_dir / "_extra"
        if selected["id_index"] is not None:
            id_path = item.get("out_mask") or str(extra / "{}_遮罩_ID.jpg".format(no))
            destinations.append((selected["id_index"], id_path, "遮罩_ID"))
            self.log("遮罩_ID 使用{}，管理器索引 {}".format(
                "精确匹配" if selected["id_exact"] else "模糊匹配", selected["id_index"]))
        if selected["wire_index"] is not None:
            wire_path = (self._calibration_dir() / "regions" / "{}.png".format(no)
                         if self.job.get("want_manifest") else extra / "{}_遮罩_线框颜色.jpg".format(no))
            destinations.append((selected["wire_index"], str(wire_path), "遮罩_线框颜色"))
        routed = {selected["id_index"], selected["wire_index"]}
        for element in elements:
            if element["index"] not in routed:
                destinations.append((element["index"], str(extra / "{}_元素{}.jpg".format(no, element["index"])),
                                     "其他渲染元素{}".format(element["index"])))
        for index, destination, label in destinations:
            try:
                if destination and Path(destination) == Path(item["out_image"]):
                    raise ValueError("遮罩输出路径与成图路径相同")
                suffix = ".png" if label == "遮罩_线框颜色" and self.job.get("want_manifest") else ".jpg"
                path = self._output(destination, suffix)
                route = {"index": index, "path": path, "label": label, "message": ""}
                routes.append(route)
                if not manager.GetElementsActive() or index not in active_indices:
                    route["message"] = "模板的渲染元素总开关或该元素未启用，保持原设置"
                try:
                    original = manager.GetRenderElementFilename(index)
                    restore_paths.append((manager, index, original))
                    manager.SetRenderElementFilename(index, path)
                    self.log("{} 输出到：{}".format(label, path))
                except Exception:
                    route["message"] = "设置输出路径失败，将尝试位图兜底：" + traceback.format_exc()
            except Exception:
                messages.append("{} 输出准备失败：{}".format(label, traceback.format_exc()))
        return {"manager": manager, "active_indices": active_indices,
                "routes": routes, "message": "；".join(messages)}

    def _save_mask(self, path, bitmaps, manager, index, active_indices, reason, previous=None):
        def available():
            if previous is None:
                return Path(path).is_file() and Path(path).stat().st_size > 0
            current = core.lighting_output_stamp(path)
            return current is not None and current != previous

        if available():
            return ""
        if index is None:
            return reason
        values = list(bitmaps) if bitmaps is not None else []
        if len(values) == int(manager.NumRenderElements()):
            bitmap_index = index
        elif len(values) == len(active_indices) and index in active_indices:
            bitmap_index = active_indices.index(index)
        else:
            return "{}；遮罩未输出，renderElementBitmaps 数量无法与元素索引对应（返回 {} 个）".format(reason, len(values)).lstrip("；")
        bitmap = values[bitmap_index]
        if bitmap is None:
            return "{}；renderElementBitmaps 对应遮罩位图为空".format(reason).lstrip("；")
        try:
            bitmap.filename = path
            rt.save(bitmap)
        except Exception:
            return "{}；遮罩位图另存失败：{}".format(reason, traceback.format_exc()).lstrip("；")
        if not available():
            return "{}；遮罩位图另存后仍无本轮有效文件".format(reason).lstrip("；")
        self.log("遮罩已通过 renderElementBitmaps 兜底保存")
        return ""

    def _camera(self, item, origin, scale):
        spec = item["camera"]
        if spec.get("frame") != "PF1":
            raise ValueError("只接受 PF1 相机")
        pos = core.pf1_to_max(spec["position_mm"], origin, scale)
        target_pos = core.pf1_to_max(spec["target_mm"], origin, scale)
        if math.hypot(pos[0] - target_pos[0], pos[1] - target_pos[1]) < 1e-9:
            raise ValueError("相机与目标重合或完全垂直，无法确定无横滚地平线")
        correction = core.camera_vertical_correction(spec, item["width"], item["height"])
        if correction is not None:
            return self._physical_camera(item, pos, origin, scale, correction)
        hfov = core.horizontal_fov_deg(spec["fov_y_deg"], item["width"], item["height"])
        target = rt.TargetObject(name="WhiteBG_目标_{}".format(item["no"]), pos=rt.Point3(*target_pos))
        try:
            camera = rt.TargetCamera(name="WhiteBG_相机_{}".format(item["no"]),
                                     pos=rt.Point3(*pos), target=target)
            camera.fovType = 1  # Max 角度属性以度赋值；不要转为弧度。
            camera.fov = hfov
            # 新建 Target Camera 的默认 LookAt 以世界 Z 向上，默认 roll 为 0。
            if rt.hasProperty(camera, "rollAngle"):
                camera.rollAngle = 0.0
            return camera, target
        except Exception:
            rt.delete(target)
            raise

    def _physical_camera(self, item, pos, origin, scale, correction):
        target_pos = core.pf1_to_max(correction["target_mm"], origin, scale)
        # 新建 Physical 相机时 targeted=True 不会自动生成目标点（实测 camera.target 为 None），
        # 与 TargetCamera 一样先建目标点再传入。
        target = rt.TargetObject(name="WhiteBG_目标_{}".format(item["no"]), pos=rt.Point3(*target_pos))
        try:
            camera = rt.Physical(name="WhiteBG_相机_{}".format(item["no"]), pos=rt.Point3(*pos),
                                 targeted=True, target=target)
        except Exception:
            rt.delete(target)
            raise
        try:
            camera.auto_vertical_tilt_correction = False
            camera.use_dof = False
            camera.vignetting_enabled = False
            camera.specify_fov = True
            camera.fov = correction["horizontal_fov_deg"]
            # MAXScript 的属性使用百分数，界面数值与脚本数值相同。
            camera.vertical_shift = 100 * correction["image_shift_film_width"]
            return camera, target
        except Exception:
            rt.delete(camera)
            if rt.isValidNode(target):
                rt.delete(target)
            raise

    def _render_item(self, item, origin, scale):
        camera = target = beauty = elements = mask = None
        restore_paths = []
        try:
            width, height = item["width"], item["height"]
            if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in (width, height)):
                raise ValueError("输出宽高必须为正整数")
            rt.renderWidth, rt.renderHeight, rt.renderPixelAspect = width, height, 1.0
            camera, target = self._camera(item, origin, scale)
            out_image = self._output(item["out_image"], ".jpg")
            sweep = self.job["mode"] == "smallshot" and self._lighting_tuning()["capture_mask"]
            previous_image = core.lighting_output_stamp(out_image) if sweep else None

            def available_image():
                if not sweep:
                    return Path(out_image).is_file() and Path(out_image).stat().st_size > 0
                stamp = core.lighting_output_stamp(out_image)
                return stamp is not None and stamp != previous_image

            mask = None
            reason = ""
            # 任何出图都要先把所有启用的渲染元素改到任务目录：模板里元素的旧输出路径不存在时，
            # 隐藏的 Max 会弹"图像 I/O 错误"并卡住（2026-11-20 关掉 AI 校准出小样时实测复现）。
            try:
                mask = self._mask_setup(item, restore_paths)
            except Exception:
                reason = "遮罩准备失败：" + traceback.format_exc()
            previous_masks = {route["path"]: core.lighting_output_stamp(route["path"])
                              for route in mask["routes"] if route["label"] == "遮罩_ID"} if sweep and mask else {}
            self._touch()
            render_started = time.time()
            if self.job["mode"] == "final" or self.job.get("want_manifest") or sweep:
                # 一次渲染同时接收位图；自动输出失败后再另存，不进行第二次渲染。
                beauty, elements = rt.render(camera=camera, frame=0, outputFile=out_image,
                                             vfb=False, quiet=True, renderElementBitmaps=pymxs.byref(None))
            else:
                beauty = rt.render(camera=camera, frame=0, outputFile=out_image, vfb=False, quiet=True)
            if beauty is None:
                raise RuntimeError("渲染没有返回成图位图，可能被取消")
            self._check_not_blank(beauty, render_started, out_image)
            if not available_image():
                beauty.filename = out_image
                rt.save(beauty)
            if not available_image():
                raise RuntimeError("渲染后未生成本轮成图")
            if mask is not None:
                messages = [mask["message"]] if mask["message"] else []
                for route in mask["routes"]:
                    try:
                        message = self._save_mask(route["path"], elements, mask["manager"],
                                                  route["index"], mask["active_indices"], route["message"],
                                                  previous_masks.get(route["path"]))
                        if message:
                            messages.append("{}：{}".format(route["label"], message))
                    except Exception:
                        messages.append("{} 兜底失败：{}".format(route["label"], traceback.format_exc()))
                reason = "；".join(messages)
            return reason
        finally:
            for manager, index, original_filename in reversed(restore_paths):
                try:
                    manager.SetRenderElementFilename(index, original_filename)
                except Exception:
                    self.log("恢复遮罩输出路径失败：" + traceback.format_exc())
            bitmaps = ([beauty] if beauty is not None else []) + (list(elements) if elements is not None else [])
            for bitmap in bitmaps:
                if bitmap is not None:
                    try:
                        rt.close(bitmap)
                    except Exception:
                        self.log("关闭渲染位图失败：" + traceback.format_exc())
            for node in (camera, target):
                if node is not None and rt.isValidNode(node):
                    rt.delete(node)

    def _corona_error_log(self):
        """Corona 的错误日志在 Max 的 plugcfg\\corona 下，文件名随配置名变（如「CR 混合2025_errors.txt」）。"""
        try:
            folder = Path(str(rt.getDir(rt.Name("plugcfg")))) / "corona"
            logs = sorted(folder.glob("*_errors.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
            return logs[0] if logs else None
        except Exception:
            self.log("找不到 Corona 错误日志：" + traceback.format_exc())
            return None

    def _check_not_blank(self, beauty, started, out_image):
        """抽查几行像素；全黑就判这一张失败，删掉黑图，并尽量从 Corona 错误日志里给出原因。"""
        try:
            width, height = int(beauty.width), int(beauty.height)
            samples = []
            for y in core.sample_rows(height):
                pixels = rt.getPixels(beauty, rt.Point2(0, y), width)
                for i in range(0, len(pixels), 16):
                    color = pixels[i]
                    samples.append((color.r, color.g, color.b))
        except Exception:
            self.log("抽查成图像素失败，跳过全黑检查：" + traceback.format_exc())
            return
        if not core.is_blank(samples):
            return
        corona_error = core.corona_error_since(self._corona_error_log(), started)
        if corona_error:
            self.log("Corona 错误日志：" + corona_error)
        try:
            Path(out_image).unlink(missing_ok=True)
        except OSError:
            self.log("删除全黑成图失败：" + traceback.format_exc())
        raise BlankRenderError(core.blank_render_message(corona_error))

    def render_job(self):
        self._overrides = None
        self._scene_manifest = None
        self._calibration_unsupported = []
        self._correction_sources = {}
        if self.job.get("mode") not in ("smallshot", "final"):
            raise ValueError("未知渲染模式")
        if not self.job.get("items"):
            raise ValueError("渲染任务没有明细")
        anchor = core.template_anchor_from(self.job, self.config)
        template = self.job.get("template_max", self.config.get("template_max"))
        self._load(template)
        self._remove_template_cameras()
        scale = self._mm_per_unit()
        template_objects = list(rt.objects)
        template_nodes = self._geometry(template_objects, visible_only=False)
        try:
            self._template_room = self._bbox(template_nodes) if template_nodes else None
        except Exception:
            self._template_room = None
            self.log("读取模板布景范围失败，主光距离不做收进：" + traceback.format_exc())
        calibration = self.job.get("overrides") is not None or self.job.get("want_manifest")
        # 合并前收集模板灯，禁止把产品带入的灯光当成模板灯调整。
        template_lights = [node for node in template_objects if rt.superClassOf(node) == rt.Light]
        self._template_lights = template_lights
        tuning = self._lighting_tuning()
        self._lighting_background_boxes = (self._lighting_background(template_objects)
                                           if tuning["probe"] or tuning["bg_fill_keep"] else {})
        merged = self._merge(self.job["max_file"])
        if "keep_nodes" in self.job:
            merged, meshes = self._keep_variant_nodes(merged)
        else:
            meshes = self._geometry(merged, visible_only=True)
        bounds = self._bbox(meshes)
        placement = core.placement_from_bbox(*bounds, anchor, scale)
        self._move_roots(merged, placement["delta_max"])
        # 用移动后的实测包围盒验证控制器是否接受位移，避免以错误原点继续渲染。
        placed_bounds = self._bbox(meshes)
        actual_origin = core.bbox_center(*placed_bounds)
        tolerance = core.mm_to_units(0.01, scale)
        if any(abs(a - b) > tolerance for a, b in zip(actual_origin, placement["placed_origin_max"])):
            raise RuntimeError("合并对象的控制器未按预期接受锚点位移")
        self.log("产品已贴墙摆放（Max 毫米）：{}".format(anchor))
        self._material_log(meshes)
        preset = self.job.get("render_preset_rps", self.config.get("render_preset_rps"))
        if preset and rt.renderPresets.LoadAll(0, self._source(preset)) is not True:
            raise RuntimeError("渲染预设载入失败")
        if tuning["probe"]:
            self._probe_lighting()
        if calibration:
            center = core.bottom_center(*self._bbox(meshes))
            self._apply_calibration(meshes, template_lights, center, scale)
        with self._smallshot_time():
            for item, item_status in zip(self.job["items"], self.status["items"]):
                item_status.update(state="rendering", started_at=core.timestamp(), message="")
                self.flush()
                try:
                    with self._item_lights(item, placed_bounds, scale), self._back_view_visibility(item, template_nodes, anchor, scale):
                        message = self._render_item(item, actual_origin, scale)
                    item_status.update(state="done", message=message)
                    self.log("明细 {} 已保存{}".format(item["no"], "；" + message if message else ""))
                except Exception:
                    error = sys.exc_info()[1]
                    detail = str(error) if isinstance(error, BlankRenderError) else traceback.format_exc()
                    message = "明细 {} 失败：{}".format(item["no"], detail)
                    item_status.update(state="failed", message=message)
                    self.log(message)
                    if isinstance(sys.exc_info()[1], SceneRestoreError):
                        raise
                finally:
                    # 成功、渲染异常及恢复异常后均刷新，失败则停止继续处理。
                    self._touch()
                self.flush()


def _configure(config_path=None):
    path = Path(config_path).resolve() if config_path else _HERE.parent / "config.json"
    try:
        runner = Runner(path)
    except Exception:
        # 配置不可读时没有可靠 exchange_dir，只能记在规格默认 exchange/。
        try:
            core.append_runner_log(path.parent / "exchange", "执行器配置失败：" + traceback.format_exc())
        except Exception:
            sys.stderr.write("白底执行器启动失败且日志不可写：" + traceback.format_exc() + "\n")
        return None
    _session.runner = runner
    return runner


def start(config_path):
    """启动单个 3 秒主线程定时器；重复执行启动脚本也复用会话状态。"""
    try:
        app = _main_thread()
        if _session.timer is not None and _session.timer.isActive():
            _session.runner.log("执行器已启动，复用现有定时器与配置")
            return False
        if _session.busy:
            _session.runner.log("任务正在执行，不能更换配置或重新启动")
            return False
        runner = _configure(config_path)
        if runner is None:
            return False
        if _session.timer is not None:
            _session.timer.deleteLater()
        _session.timer = QtCore.QTimer(app)
        _session.timer.timeout.connect(run_once)
        _session.timer.setInterval(3000)
        if not getattr(_session, "quit_connected", False):
            app.aboutToQuit.connect(_stop_heartbeat)
            _session.quit_connected = True
        runner.heartbeat = core.RunnerHeartbeat(runner.exchange, RUNNER_VERSION, runner.log_file)
        runner.heartbeat.start()
        _session.timer.start()
        runner.log("执行器已启动，每 3 秒检查任务")
        return True
    except Exception:
        _startup_error(config_path, "启动执行器失败：" + traceback.format_exc())
        return False


def _startup_error(config_path, message):
    if _session.runner is not None:
        _session.runner.log(message)
    else:
        try:
            path = Path(config_path).resolve() if config_path else _HERE.parent / "config.json"
            core.append_runner_log(path.parent / "exchange", message)
        except Exception:
            sys.stderr.write(message + "\n" + traceback.format_exc())


def _stop_heartbeat():
    heartbeat = getattr(_session.runner, "heartbeat", None)
    if heartbeat is not None:
        heartbeat.stop()


def stop():
    """停止后续轮询；同步渲染在安全边界完成、清场并释放锁。"""
    try:
        _main_thread()
        if _session.timer is not None:
            _session.timer.stop()
        _stop_heartbeat()
        if _session.runner is not None:
            _session.runner.log("执行器已停止轮询；正在执行的任务会完成清理")
        return True
    except Exception:
        _startup_error(None, "停止执行器失败：" + traceback.format_exc())
        return False


def run_once(config_path=None):
    """同步处理一个任务；默认使用 start 的配置，也可直接传配置调试。"""
    if _session.busy:
        return None
    runner = None
    try:
        _main_thread()
        _session.busy = True
        if config_path is not None:
            if _session.timer is not None and _session.timer.isActive():
                _session.runner.log("定时器运行期间不能通过 run_once 更换配置，请先 stop")
                return None
            runner = _configure(config_path)
        else:
            runner = _session.runner or _configure()
        if runner is not None:
            if runner.heartbeat is not None:
                runner.heartbeat.beat()
            return runner.process_next()
    except Exception:
        message = "轮询异常：" + traceback.format_exc()
        if runner is not None:
            runner.log(message)
        else:
            _startup_error(config_path, message)
        return None
    finally:
        _session.busy = False
