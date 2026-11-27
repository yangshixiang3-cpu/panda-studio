"""1.8.0 白底纯白背景、预设保护、0~2 倍环境反射、嵌套材质、预览材质清单与队列归档跳过测试。"""

from contextlib import nullcontext
import json
import math
from pathlib import Path
import shutil
import sys
import types
import unittest
from unittest import mock
from uuid import uuid4

MAX_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = MAX_DIR.parent
TMP_DIR = REPO_ROOT / "tests" / "tmp" / "whitebg-180"
if str(MAX_DIR) not in sys.path:
    sys.path.insert(0, str(MAX_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import wb_core as core
import test_wb_core as existing


class Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class QueueArchiveFilterTests(unittest.TestCase):
    def setUp(self):
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        self.jobs_dir = TMP_DIR / ("jobs-" + uuid4().hex[:8])
        self.jobs_dir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.jobs_dir, ignore_errors=True)

    def test_pending_jobs_skips_archive_dir_index_status_and_underscore_files(self):
        archive = self.jobs_dir / "archive"
        archive.mkdir()
        core.atomic_write_json(archive / "archived-job.json", {
            "job_id": "archived-job", "created_at": "2026-11-20T08:00:00Z", "type": "export_preview",
        })
        # 即使命名带 .json 的子目录也必须跳过
        (self.jobs_dir / "folder.json").mkdir()

        core.atomic_write_json(self.jobs_dir / "index.json", {"jobs": []})
        core.atomic_write_json(self.jobs_dir / "jobs.index.json", {"jobs": []})
        core.atomic_write_json(self.jobs_dir / "_meta.json", {"version": 1})
        core.atomic_write_json(self.jobs_dir / ".staging.json", {"temp": True})
        core.atomic_write_json(self.jobs_dir / "orphan.status.json", {"state": "done"})
        core.atomic_write_json(self.jobs_dir / "valid-1.json", {
            "job_id": "valid-1", "created_at": "2026-11-26T10:00:00Z", "type": "export_preview",
        })

        errors = []
        jobs = core.pending_jobs(self.jobs_dir, lambda path, exc: errors.append((path, exc)))
        self.assertEqual([j.data["job_id"] for j in jobs], ["valid-1"])
        self.assertEqual(errors, [])


class PureWhiteAndPresetTests(unittest.TestCase):
    def setUp(self):
        existing.RunnerContractTests.setUp(self)
        self.dir = TMP_DIR / ("runner-180-" + uuid4().hex[:8])
        self.dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.runner.exchange = self.dir / "exchange"
        self.runtime.Color.side_effect = lambda r, g, b: Obj(r=float(r), g=float(g), b=float(b))
        self.runtime.getHandleByAnim.side_effect = id
        self.runtime.getPropNames.side_effect = lambda obj: list(vars(obj))
        self.runtime.getNumSubTexmaps.return_value = 0

    def test_pure_white_background_overrides_direct_color_clears_hdri_and_restores_on_error(self):
        hdri = Obj(class_name="CoronaBitmap", filename="D:/hdr/studio.hdr", name="studio.hdr")
        renderer = Obj(
            class_name="CoronaRenderer",
            bg_overrideDirect=False,
            bg_colorDirect=Obj(r=160.0, g=165.0, b=170.0),
            bg_texmapDirect=hdri,
            bg_texmapUseDirect=True,
        )
        self.runtime.renderers.current = renderer
        self.runtime.classOf.side_effect = lambda obj: getattr(obj, "class_name", "Unknown")

        with self.assertRaisesRegex(RuntimeError, "渲染中断"):
            with self.runner._pure_white_background():
                self.assertTrue(renderer.bg_overrideDirect)
                self.assertEqual((renderer.bg_colorDirect.r, renderer.bg_colorDirect.g, renderer.bg_colorDirect.b),
                                 (255.0, 255.0, 255.0))
                self.assertIsNone(renderer.bg_texmapDirect)
                self.assertFalse(renderer.bg_texmapUseDirect)
                raise RuntimeError("渲染中断")

        self.assertFalse(renderer.bg_overrideDirect)
        self.assertEqual((renderer.bg_colorDirect.r, renderer.bg_colorDirect.g, renderer.bg_colorDirect.b),
                         (160.0, 165.0, 170.0))
        self.assertIs(renderer.bg_texmapDirect, hdri)
        self.assertTrue(renderer.bg_texmapUseDirect)

    def test_back_view_hiding_wall_enforces_pure_white_direct_and_restores(self):
        renderer = Obj(
            class_name="CoronaRenderer",
            BG_OVERRIDEDIRECT=False,
            bg_colorDirect=Obj(r=120.0, g=120.0, b=120.0),
            bg_texmapDirect=Obj(class_name="CoronaSky", name="Sky"),
        )
        self.runtime.renderers.current = renderer
        self.runtime.classOf.side_effect = lambda obj: getattr(obj, "class_name", "Unknown")
        wall = Obj(name="Rectangle2139486305", isNodeHidden=False)
        self.runner._bbox = mock.Mock(return_value=((0, 471, 0), (100, 500, 200)))
        item = {"camera": {"frame": "PF1", "position_mm": [0, 0, -1500], "target_mm": [0, 0, 0]}}

        with self.runner._back_view_visibility(item, [wall], {"back_y_mm": 461}, 1):
            self.assertTrue(wall.isNodeHidden)
            self.assertTrue(renderer.BG_OVERRIDEDIRECT)
            self.assertEqual((renderer.bg_colorDirect.r, renderer.bg_colorDirect.g, renderer.bg_colorDirect.b),
                             (255.0, 255.0, 255.0))
            self.assertIsNone(renderer.bg_texmapDirect)

        self.assertFalse(wall.isNodeHidden)
        self.assertFalse(renderer.BG_OVERRIDEDIRECT)
        self.assertEqual((renderer.bg_colorDirect.r, renderer.bg_colorDirect.g, renderer.bg_colorDirect.b),
                         (120.0, 120.0, 120.0))
        self.assertIsNotNone(renderer.bg_texmapDirect)

    def test_environment_gi_darkening_bg_color_locks_pure_white_direct_even_when_already_enabled(self):
        dirty_map = Obj(class_name="Gradient", name="灰渐变")
        renderer = Obj(
            class_name="CoronaRenderer",
            BG_OVERRIDEDIRECT=True,
            bg_colorDirect=Obj(r=190.0, g=190.0, b=190.0),
            BG_COLOR=Obj(r=220.0, g=220.0, b=220.0),
            bg_texmapDirect=dirty_map,
        )
        self.runtime.renderers.current = renderer
        self.runtime.classOf.side_effect = lambda obj: getattr(obj, "class_name", "Unknown")

        snapshots = []
        bindings = {"direct_enabled": "BG_OVERRIDEDIRECT", "direct_color": "bg_colorDirect", "source_color": "BG_COLOR"}
        ratio = self.runner._apply_environment_gi(bindings, 0.5, snapshots)
        self.assertEqual(ratio, 0.5)
        self.assertTrue(renderer.BG_OVERRIDEDIRECT)
        self.assertEqual((renderer.bg_colorDirect.r, renderer.bg_colorDirect.g, renderer.bg_colorDirect.b),
                         (255.0, 255.0, 255.0))
        self.assertIsNone(renderer.bg_texmapDirect)
        self.assertEqual((renderer.BG_COLOR.r, renderer.BG_COLOR.g, renderer.BG_COLOR.b),
                         (110.0, 110.0, 110.0))

        errors = self.runner._restore_environment_gi(snapshots)
        self.assertEqual(errors, [])
        self.assertTrue(renderer.BG_OVERRIDEDIRECT)
        self.assertEqual((renderer.bg_colorDirect.r, renderer.bg_colorDirect.g, renderer.bg_colorDirect.b),
                         (190.0, 190.0, 190.0))
        self.assertIs(renderer.bg_texmapDirect, dirty_map)
        self.assertEqual((renderer.BG_COLOR.r, renderer.BG_COLOR.g, renderer.BG_COLOR.b),
                         (220.0, 220.0, 220.0))

    def test_preset_load_restores_lost_or_disabled_mask_elements_and_reflect_override(self):
        preset_file = self.dir / "白底图4000.rps"
        preset_file.write_text("rps", encoding="utf-8")

        id_elem = Obj(class_name="CMasking_ID", elementName="遮罩_ID", enabled=True)
        wire_elem = Obj(class_name="CMasking_WireColor", elementName="遮罩_线框颜色", enabled=True)
        elements = [id_elem, wire_elem]
        active_state = [True]

        manager = self.runtime.maxOps.GetCurRenderElementMgr.return_value
        manager.GetElementsActive.side_effect = lambda: active_state[0]
        manager.SetElementsActive.side_effect = lambda flag: active_state.__setitem__(0, bool(flag))
        manager.NumRenderElements.side_effect = lambda: len(elements)
        manager.GetRenderElement.side_effect = lambda idx: elements[idx]
        manager.AddRenderElement.side_effect = lambda elem: elements.append(elem)

        reflect_tex = Obj(class_name="CoronaBitmap", name="模板反射HDR")
        tpl_renderer = Obj(class_name="CoronaRenderer", bg_overrideReflect=True, bg_texmapReflect=reflect_tex)
        preset_renderer = Obj(class_name="CoronaRenderer", bg_overrideReflect=False, bg_texmapReflect=None)
        self.runtime.renderers.current = tpl_renderer
        self.runtime.classOf.side_effect = lambda obj: getattr(obj, "class_name", "Unknown")

        def load_all(slot, path):
            # 模拟 .rps 预设删掉遮罩_ID、禁用遮罩_线框颜色、关闭总开关，并换上无反射贴图的新渲染器
            elements[:] = [wire_elem]
            wire_elem.enabled = False
            active_state[0] = False
            self.runtime.renderers.current = preset_renderer
            return True

        self.runtime.renderPresets.LoadAll.side_effect = load_all
        self.runner._load_render_preset(str(preset_file))

        self.assertIn(id_elem, elements)
        self.assertIn(wire_elem, elements)
        self.assertTrue(id_elem.enabled)
        self.assertTrue(wire_elem.enabled)
        self.assertTrue(active_state[0])
        self.assertIs(preset_renderer.bg_texmapReflect, reflect_tex)
        self.assertTrue(preset_renderer.bg_overrideReflect)


class ReflectionRangeAndNestedMaterialsTests(unittest.TestCase):
    def setUp(self):
        existing.RunnerContractTests.setUp(self)
        self.dir = TMP_DIR / ("mat-180-" + uuid4().hex[:8])
        self.dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.runner.exchange = self.dir / "exchange"
        self.runtime.Color.side_effect = lambda r, g, b: Obj(r=float(r), g=float(g), b=float(b))
        self.runtime.getHandleByAnim.side_effect = id
        self.runtime.getPropNames.side_effect = lambda obj: list(vars(obj))
        self.runtime.getNumSubTexmaps.return_value = 0
        self.runtime.classOf.side_effect = lambda obj: getattr(obj, "class_name", "CoronaPhysicalMtl")

    def test_clamp_overrides_allows_zero_to_two_reflection_strength(self):
        self.assertEqual(core.clamp_overrides({"environment": {"reflection_strength_mul": 0}})["environment"]["reflection_strength_mul"], 0.0)
        self.assertEqual(core.clamp_overrides({"environment": {"reflection_strength_mul": -0.5}})["environment"]["reflection_strength_mul"], 0.0)
        self.assertEqual(core.clamp_overrides({"environment": {"reflection_strength_mul": 0.25}})["environment"]["reflection_strength_mul"], 0.25)
        self.assertEqual(core.clamp_overrides({"environment": {"reflection_strength_mul": 2.5}})["environment"]["reflection_strength_mul"], 2.0)

    def test_zero_reflection_disables_bg_override_reflect_during_job_and_restores_after(self):
        reflect_tex = Obj(class_name="CoronaBitmap", name="studio.hdr")
        renderer = Obj(class_name="CoronaRenderer", bg_overrideReflect=True, bg_texmapReflect=reflect_tex)
        self.runtime.renderers.current = renderer
        self.runner.job.update(overrides={"environment": {"reflection_strength_mul": 0}})

        observed_in_job = []
        with self.runner._calibration_environment_scope():
            self.runner._apply_calibration([], [], (0, 0, 0), 1)
            observed_in_job.append((renderer.bg_overrideReflect, renderer.bg_texmapReflect))

        self.assertEqual(observed_in_job, [(False, reflect_tex)])
        self.assertTrue(renderer.bg_overrideReflect)
        self.assertIs(renderer.bg_texmapReflect, reflect_tex)

    def test_positive_low_reflection_multiplier_scales_texmap_and_restores_after_scope(self):
        reflect_tex = Obj(class_name="CoronaBitmap", name="studio.hdr")
        renderer = Obj(class_name="CoronaRenderer", bg_overrideReflect=False, bg_texmapReflect=reflect_tex)
        self.runtime.renderers.current = renderer
        correction = Obj(class_name="Corona色彩校正", inputTexmap=None, exposure=0.0)
        self.runtime.CoronaColorCorrect.return_value = correction
        glass = Obj(name="长虹玻璃", class_name="_Corona物理材质", baseColor=Obj(r=180, g=200, b=205),
                    baseIor=1.52, refractionamount=0.9)
        node = Obj(name="玻璃门", material=glass)
        self.runner.job.update(overrides={"environment": {"reflection_strength_mul": 0.25}})

        with self.runner._calibration_environment_scope():
            self.runner._apply_calibration([node], [], (0, 0, 0), 1)
            self.assertTrue(renderer.bg_overrideReflect)
            self.assertIs(renderer.bg_texmapReflect, correction)
            self.assertIs(correction.inputTexmap, reflect_tex)
            self.assertAlmostEqual(correction.exposure, -2.0)

        self.assertFalse(renderer.bg_overrideReflect)
        self.assertIs(renderer.bg_texmapReflect, reflect_tex)

    def test_nested_multimaterial_and_rayswitch_in_both_orders_extract_and_adjust_all_leaves(self):
        # 顺序 1：Multimaterial -> Multimaterial -> RaySwitch -> (Physical + Legacy)
        oak_tex = Obj(class_name="Bitmaptexture", filename="D:/maps/mocha_oak.jpg")
        oak_phys = Obj(name="红橡物理", class_name="_Corona物理材质",
                       baseColor=Obj(r=160, g=110, b=80), basetexmap=oak_tex,
                       baseIor=1.5, baseroughness=0.7, roughnessMode=1)
        oak_gi = Obj(name="红橡GI", class_name="Corona传统材质",
                     colorDiffuse=Obj(r=150, g=100, b=70), levelReflect=0.4, reflectGlossiness=0.5)
        oak_ray = Obj(name="摩卡红橡", class_name="Corona光线切换",
                      directMtlOn=True, directMtl=oak_phys, giMtlOn=True, giMtl=oak_gi)
        inner_multi = Obj(name="内层多维", class_name="Multimaterial", materialList=[oak_ray])
        outer_multi = Obj(name="外层多维", class_name="Multimaterial", materialList=[inner_multi])

        # 顺序 2：RaySwitch -> Multimaterial -> [RaySwitch(玻璃), Legacy(奶油白)] + giMtl
        glass_phys = Obj(name="玻璃物理", class_name="_Corona物理材质",
                         baseColor=Obj(r=180, g=200, b=205), baseIor=1.5, refractionamount=0.8, opacityLevel=1.0)
        glass_gi = Obj(name="玻璃内部", class_name="Corona传统材质",
                       colorDiffuse=Obj(r=180, g=200, b=205), IOR=1.5, levelRefract=0.8, levelOpacity=1.0)
        glass_ray = Obj(name="长虹玻璃", class_name="Corona光线切换",
                        directMtlOn=True, directMtl=glass_phys, giMtlOn=True, giMtl=glass_gi)
        white_leg = Obj(name="奶油白", class_name="Corona传统材质",
                        colorDiffuse=Obj(r=230, g=225, b=210), levelReflect=0.3, reflectGlossiness=0.75)
        sub_multi = Obj(name="门板多维", class_name="Multimaterial",
                        materialList=[glass_ray, white_leg], materialIDList=[1, 2])
        wrapper_gi = Obj(name="防溢色GI", class_name="Corona传统材质",
                         colorDiffuse=Obj(r=240, g=240, b=240), levelReflect=0.2)
        ray_wrapping_multi = Obj(name="防溢色外层", class_name="Corona光线切换",
                                 directMtlOn=True, directMtl=sub_multi, giMtlOn=True, giMtl=wrapper_gi)

        # 验证任意嵌套下 _material_data 均能准确提取
        outer_multi_data = self.runner._material_data(outer_multi)
        self.assertEqual((outer_multi_data["kind"], outer_multi_data["bitmap"]), ("wood", "mocha_oak.jpg"))
        ray_multi_data = self.runner._material_data(ray_wrapping_multi)
        self.assertEqual((ray_multi_data["kind"], ray_multi_data["color"]), ("glass", [180.0, 200.0, 205.0]))
        self.assertIn("refractionamount", ray_multi_data["params"])

        self.runtime.snapshotAsMesh.return_value = object()
        self.runtime.getNumFaces.return_value = 3
        self.runtime.getFaceMatID.side_effect = lambda mesh, idx: [1, 1, 2][idx - 1]
        self.runtime.CoronaColorCorrect.side_effect = lambda: Obj(
            class_name="Corona色彩校正", inputTexmap=None, exposure=0.0, saturation=0.0)
        self.runtime.renderers.current = Obj(class_name="CoronaRenderer", bg_texmapReflect=None)

        nodes = [Obj(name="柜体", material=outer_multi), Obj(name="柜门", material=ray_wrapping_multi)]
        self.runner.job.update(
            want_manifest=True,
            calibration_round=0,
            overrides={
                "materials": {
                    "摩卡红橡": {"brightness_mul": 2.0, "reflection_mul": 1.5, "glossiness_delta": 0.1},
                    "长虹玻璃": {"ior": 1.7, "refraction_mul": 0.5, "opacity_mul": 0.6},
                    "奶油白": {"reflection_mul": 2.0},
                }
            },
        )
        self.runner._bbox = mock.Mock(return_value=((0, 0, 0), (10, 10, 10)))
        self.runner._apply_calibration(nodes, [], (0, 0, 0), 1)

        # 验证所有嵌套子材质均已收到对应参数调整
        self.assertAlmostEqual(oak_phys.basetexmap.exposure, 1.0)
        self.assertAlmostEqual(oak_phys.baseIor, core.physical_reflection_ior(1.5, 1.5))
        self.assertAlmostEqual(oak_phys.baseroughness, 0.6)
        self.assertAlmostEqual(oak_gi.levelReflect, 0.6)
        self.assertAlmostEqual(oak_gi.reflectGlossiness, 0.6)

        self.assertEqual((glass_phys.baseIor, glass_phys.refractionamount, glass_phys.opacityLevel), (1.7, 0.4, 0.6))
        self.assertEqual((glass_gi.IOR, glass_gi.levelRefract, glass_gi.levelOpacity), (1.7, 0.4, 0.6))
        self.assertAlmostEqual(white_leg.levelReflect, 0.6)

        # 验证清单和图例准确包含三个实际材质，且外层容器名称也可调
        manifest = core.read_json(self.runner._calibration_dir() / "scene_manifest.json")
        self.assertEqual([m["name"] for m in manifest["materials"]], ["摩卡红橡", "长虹玻璃", "奶油白"])
        legend = core.read_json(self.runner._calibration_dir() / "regions" / "legend.json")
        self.assertEqual(set(legend.values()), {"摩卡红橡", "长虹玻璃"})

        # 外层 RaySwitch 容器直接调用 _apply_material 时，穿透 Multi 与 GI 子材质，且各子材质保留各自 kind
        white_leg.IOR = 1.52
        wrapper_gi.IOR = 1.50
        self.runner._apply_material(ray_wrapping_multi, {"reflection_mul": 0.5, "ior": 1.78})
        self.assertAlmostEqual(white_leg.levelReflect, 0.3)
        self.assertAlmostEqual(wrapper_gi.levelReflect, 0.1)
        self.assertEqual(glass_phys.baseIor, 1.78)
        self.assertEqual(white_leg.IOR, 1.52)
        self.assertEqual(wrapper_gi.IOR, 1.50)


class PreviewManifestAndFakeRunnerTests(unittest.TestCase):
    def setUp(self):
        existing.RunnerContractTests.setUp(self)
        self.dir = TMP_DIR / ("manifest-180-" + uuid4().hex[:8])
        self.dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.runner.exchange = self.dir / "exchange"
        self.runtime.Color.side_effect = lambda r, g, b: Obj(r=float(r), g=float(g), b=float(b))
        self.runtime.getHandleByAnim.side_effect = id
        self.runtime.getPropNames.side_effect = lambda obj: list(vars(obj))
        self.runtime.getNumSubTexmaps.return_value = 0
        self.runtime.getNumSubMtls.return_value = 0
        self.runtime.classOf.side_effect = lambda obj: getattr(obj, "class_name", "CoronaPhysicalMtl")

    def test_export_preview_writes_preview_scene_manifest(self):
        max_file = self.dir / "product.max"
        max_file.write_bytes(b"max")
        task_dir = self.runner.exchange / "tasks" / "任务1"
        out_fbx = task_dir / "preview" / "preview.fbx"
        out_bbox = task_dir / "preview" / "bbox.json"
        self.runner.job = {
            "type": "export_preview", "job_id": "job-p", "task_id": "任务1",
            "max_file": str(max_file), "out_fbx": str(out_fbx), "out_bbox": str(out_bbox),
        }
        wood_tex = Obj(class_name="Bitmaptexture", filename="D:/maps/oak.jpg")
        wood = Obj(name="摩卡红橡", class_name="_Corona物理材质",
                   baseColor=Obj(r=180, g=140, b=105), basetexmap=wood_tex,
                   baseroughness=0.7, baseIor=1.507)
        mesh = Obj(name="顶板", material=wood, isHiddenInVpt=False,
                   min=Obj(x=0, y=0, z=0), max=Obj(x=100, y=50, z=80))
        self.runtime.objects = [mesh]
        self.runtime.renderers.current = Obj(class_name="CoronaRenderer",
                                             bg_texmapReflect=Obj(name="env_studio.hdr"))
        self.runner._load = mock.Mock()
        self.runner._geometry = mock.Mock(return_value=[mesh])
        self.runner._is_staging = mock.Mock(return_value=False)
        self.runner._bbox = mock.Mock(return_value=((0, 0, 0), (100, 50, 80)))
        self.runner._mm_per_unit = mock.Mock(return_value=1.0)
        self.runner._touch = mock.Mock()
        self.runtime.FBXExporterGetParam.return_value = False
        self.runtime.FBXExporterSetParam.return_value = True

        def fake_export(path, *args, **kwargs):
            Path(path).write_bytes(b"FBX")
            return True

        self.runtime.exportFile.side_effect = fake_export
        self.runner.export_preview()

        manifest_path = task_dir / "preview" / "scene_manifest.json"
        self.assertTrue(manifest_path.is_file())
        manifest = core.read_json(manifest_path)
        self.assertEqual(manifest["environment"]["reflect_map"], "env_studio.hdr")
        self.assertEqual(len(manifest["materials"]), 1)
        self.assertEqual(manifest["materials"][0]["name"], "摩卡红橡")
        self.assertEqual(manifest["materials"][0]["kind"], "wood")
        self.assertEqual(manifest["materials"][0]["bitmap"], "oak.jpg")
        self.assertIn("baseroughness", manifest["materials"][0]["params"])

    def test_render_job_without_want_manifest_still_writes_preview_scene_manifest(self):
        anchor = {"x_mm": 0, "back_y_mm": 0, "floor_z_mm": 0}
        camera = {"frame": "PF1", "position_mm": [0, 0, 1000], "target_mm": [0, 0, 0]}
        white = Obj(name="奶油白", class_name="Corona传统材质",
                    colorDiffuse=Obj(r=230, g=225, b=210), levelReflect=0.4, reflectGlossiness=0.78)
        mesh = Obj(name="门板", material=white)
        self.runner.job = {
            "mode": "smallshot", "task_id": "任务2", "template_max": "模板.max", "max_file": "产品.max",
            "template_anchor": anchor, "want_manifest": False,
            "items": [{"no": 1, "camera": camera}],
        }
        self.runner.status = {"items": [{"state": "pending"}]}
        self.runtime.objects = []
        self.runtime.renderers.current = Obj(class_name="CoronaRenderer",
                                             bg_texmapReflect=Obj(name="reflect.hdr"),
                                             bg_overrideDirect=True,
                                             bg_colorDirect=Obj(r=255, g=255, b=255))
        self.runner._load = self.runner._remove_template_cameras = mock.Mock()
        self.runner._mm_per_unit = mock.Mock(return_value=1.0)
        self.runner._geometry = mock.Mock(side_effect=[[], [mesh]])
        self.runner._merge = mock.Mock(return_value=[mesh])
        self.runner._bbox = mock.Mock(return_value=((-10, -20, 0), (10, 0, 20)))
        self.runner._move_roots = self.runner._material_log = self.runner._touch = mock.Mock()
        self.runner._smallshot_time = nullcontext
        self.runner._render_item = mock.Mock(return_value="")

        self.runner.render_job()

        preview_manifest = self.runner.exchange / "tasks" / "任务2" / "preview" / "scene_manifest.json"
        self.assertTrue(preview_manifest.is_file())
        data = core.read_json(preview_manifest)
        self.assertEqual(data["environment"]["reflect_map"], "reflect.hdr")
        self.assertEqual([m["name"] for m in data["materials"]], ["奶油白"])
        self.assertEqual(data["materials"][0]["kind"], "solid")
        self.assertFalse((self.runner.exchange / "tasks" / "任务2" / "calibration").exists())

    def test_fake_runner_writes_preview_manifest_on_preview_and_ordinary_render_and_skips_archive(self):
        from tools.fake_runner import run_once as fake_run_once

        exchange = self.dir / "fake_exchange"
        jobs_dir = exchange / "jobs"
        jobs_dir.mkdir(parents=True)
        (jobs_dir / "archive").mkdir()
        core.atomic_write_json(jobs_dir / "archive" / "old.json", {
            "type": "export_preview", "job_id": "old", "task_id": "t-old", "created_at": "2026-11-01T00:00:00Z",
        })
        core.atomic_write_json(jobs_dir / "index.json", {"archived": 1})
        core.atomic_write_json(jobs_dir / "_temp.json", {"ignore": True})

        fixture = self.dir / "fixture.fbx"
        fixture.write_bytes(b"FBX_FIXTURE")

        # 1) export_preview 生成 preview/scene_manifest.json
        task1 = exchange / "tasks" / "task-preview"
        core.atomic_write_json(jobs_dir / "job-prev.json", {
            "type": "export_preview", "job_id": "job-prev", "task_id": "task-preview",
            "created_at": "2026-11-26T10:00:00+08:00",
            "max_file": str(fixture),
            "out_fbx": str(task1 / "preview" / "preview.fbx"),
            "out_bbox": str(task1 / "preview" / "bbox.json"),
        })
        # 2) 普通 render (want_manifest=False) 也生成 preview/scene_manifest.json
        task2 = exchange / "tasks" / "task-render"
        core.atomic_write_json(jobs_dir / "job-ren.json", {
            "type": "render", "mode": "smallshot", "job_id": "job-ren", "task_id": "task-render",
            "created_at": "2026-11-26T10:01:00+08:00",
            "want_manifest": False,
            "items": [{
                "no": 1, "width": 80, "height": 60,
                "camera": {"frame": "PF1", "position_mm": [0, 0, 1000], "target_mm": [0, 0, 0],
                           "azimuth_deg": 35.0, "elevation_deg": 12.0},
                "out_image": str(task2 / "smallshots" / "1.jpg"),
                "out_mask": None,
            }],
        })

        processed = fake_run_once(exchange, fixture)
        self.assertEqual(processed, 2)
        for task_folder in (task1, task2):
            manifest_file = task_folder / "preview" / "scene_manifest.json"
            self.assertTrue(manifest_file.is_file())
            manifest = core.read_json(manifest_file)
            by_name = {m["name"]: m for m in manifest["materials"]}
            self.assertEqual(set(by_name), {"摩卡红橡", "奶油白", "长虹玻璃"})
            self.assertEqual(by_name["摩卡红橡"]["kind"], "wood")
            self.assertEqual(by_name["奶油白"]["kind"], "solid")
            self.assertEqual(by_name["长虹玻璃"]["kind"], "glass")
            self.assertTrue(by_name["摩卡红橡"]["params"])
            self.assertTrue(by_name["长虹玻璃"]["params"])


if __name__ == "__main__":
    unittest.main()
