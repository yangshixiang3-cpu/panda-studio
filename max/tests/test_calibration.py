"""AI 校准的离线计算与调用契约；模拟调用不能替代 Max 实测。"""

import colorsys
import math
from pathlib import Path
import sys
import types
import unittest
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wb_core as core
import test_wb_core as existing


class OverrideTests(unittest.TestCase):
    def test_null_and_empty_are_neutral(self):
        self.assertEqual(core.clamp_overrides(None), core.clamp_overrides({}))
        self.assertEqual(core.item_light_angle(core.clamp_overrides(None), 1), 0)

    def test_all_material_ranges_both_edges(self):
        for key, (low, high) in core.MATERIAL_LIMITS.items():
            with self.subTest(key=key):
                for supplied, expected in ((-1000, low), (1000, high), ((low + high) / 2, (low + high) / 2)):
                    result = core.clamp_overrides({"materials": {"胡桃木": {key: supplied}}})
                    self.assertEqual(result["materials"]["胡桃木"][key], expected)

    def test_nullable_ior_and_unknown_fields(self):
        source = {"materials": {"玻璃": {"ior": None, "metalness": 1}}, "renderer": {"passes": 1}}
        self.assertEqual(core.clamp_overrides(source)["materials"], {"玻璃": {"ior": None}})
        self.assertNotIn("renderer", core.clamp_overrides(source))
        self.assertIn("metalness", source["materials"]["玻璃"])

    def test_all_light_environment_and_item_ranges(self):
        source = {"lights": {"intensity_mul": 5, "rotate_deg": -100, "per_light": {"灯": {"intensity_mul": 8}}},
                  "environment": {"reflection_strength_mul": -3}, "items": {"1": {"lights": {"rotate_deg": 120}}}}
        result = core.clamp_overrides(source)
        self.assertEqual(result["lights"], {"intensity_mul": 1.6, "rotate_deg": -90, "per_light": {"灯": {"intensity_mul": 2}}})
        self.assertEqual(result["environment"]["reflection_strength_mul"], 0.5)
        self.assertEqual(core.item_light_angle(result, 1), 90)
        self.assertEqual(core.item_light_angle(result, 2), -90)

    def test_other_light_edges_and_explicit_zero(self):
        result = core.clamp_overrides({"lights": {"rotate_deg": 100, "intensity_mul": -1, "per_light": {"灯": {"intensity_mul": -1}}},
                                     "environment": {"reflection_strength_mul": 3}, "items": {"1": {"lights": {"rotate_deg": 0}}}})
        self.assertEqual(result["lights"]["intensity_mul"], 0.5)
        self.assertEqual(result["lights"]["per_light"]["灯"]["intensity_mul"], 0.5)
        self.assertEqual(result["environment"]["reflection_strength_mul"], 2)
        self.assertEqual(core.item_light_angle(result, 1), 0)
        self.assertEqual(core.item_light_angle(result, 2), 90)

    def test_invalid_numbers_and_shapes_rejected(self):
        for value in (True, "1", None, math.nan, math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.clamp_overrides({"lights": {"rotate_deg": value}})
        for source in ([], {"materials": []}, {"lights": {"per_light": {"灯": None}}}, {"items": {"1": []}}):
            with self.subTest(source=source), self.assertRaises(ValueError):
                core.clamp_overrides(source)


class ColorAndPropertyTests(unittest.TestCase):
    def test_hsv_neutral_preserves_color(self):
        for rgb in ((0, 0, 0), (255, 255, 255), (130, 87, 43)):
            for actual, expected in zip(core.adjust_hsv(rgb), rgb):
                self.assertAlmostEqual(actual, expected)

    def test_hue_wraps_and_preserves_value(self):
        result = core.adjust_hsv((255, 0, 0), hue_shift_deg=-15)
        self.assertAlmostEqual(result[0], 255)
        self.assertAlmostEqual(result[1], 0)
        self.assertAlmostEqual(result[2], 63.75)

    def test_saturation_brightness_and_contrast(self):
        result = core.adjust_hsv((200, 100, 100), saturation_mul=0.6, brightness_mul=0.6)
        h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in result))
        self.assertAlmostEqual(h, 0)
        self.assertAlmostEqual(s, 0.3)
        self.assertAlmostEqual(v, 120 / 255)
        self.assertEqual(core.adjust_hsv((255, 0, 0), contrast_mul=1.3), (255, 0, 0))

    def test_color_inputs_and_adjustments_are_clamped(self):
        result = core.adjust_hsv((-1, 270, 100), saturation_mul=9, brightness_mul=9, hue_shift_deg=90)
        self.assertTrue(all(0 <= c <= 255 for c in result))
        self.assertEqual(result, core.adjust_hsv((0, 255, 100), saturation_mul=1.4, brightness_mul=1.4, hue_shift_deg=15))

    def test_property_mapping_and_order_ignore_class_names(self):
        self.assertEqual(core.material_property({'levelReflect', 'reflectionLevel'}, 'reflection_mul'), 'levelReflect')
        self.assertEqual(core.material_property({'base_color'}, 'color'), 'base_color')
        self.assertIsNone(core.material_property(set(), 'color'))
        self.assertIsNone(core.material_property({'colorDiffuse'}, 'ior'))

    def test_property_callback_stops_at_first_match(self):
        exists = mock.Mock(side_effect=[False, True])
        self.assertEqual(core.pick_property(("a", "b", "c"), exists), "b")
        self.assertEqual(exists.call_args_list, [mock.call("a"), mock.call("b")])

    def test_glossiness_and_roughness_have_opposite_signs(self):
        self.assertAlmostEqual(core.adjusted_scalar(0.6, "glossiness_delta", 0.2, "reflectGlossiness"), 0.8)
        self.assertAlmostEqual(core.adjusted_scalar(0.6, "glossiness_delta", 0.2, "baseRoughness"), 0.4)
        self.assertAlmostEqual(core.adjusted_scalar(0.6, "glossiness_delta", 0.2, "baseRoughness", glossiness_mode=True), 0.8)
        self.assertEqual(core.adjusted_scalar(0.9, "glossiness_delta", 0.3, "glossiness"), 1)
        self.assertEqual(core.adjusted_scalar(0.1, "glossiness_delta", 0.3, "roughness"), 0)

    def test_scalar_multipliers_and_ior(self):
        self.assertEqual(core.adjusted_scalar(0.8, "reflection_mul", 2, "levelReflect"), 1)
        self.assertAlmostEqual(core.adjusted_scalar(0.8, "refraction_mul", 0.5, "levelRefract"), 0.4)
        self.assertAlmostEqual(core.adjusted_scalar(0.8, "opacity_mul", 0.5, "opacity"), 0.4)
        self.assertEqual(core.adjusted_scalar(1.1, "ior", 5, "baseIor"), 1.8)

    def test_correction_neutral_scales(self):
        self.assertAlmostEqual(core.color_correct_value("saturation_mul", 1.4, 0), 0.4)
        self.assertEqual(core.color_correct_value("brightness_mul", 1.4, 1), 1.4)
        self.assertEqual(core.color_correct_value("hue_shift_deg", -15, 0), -15)
        with self.assertRaises(ValueError):
            core.color_correct_value("contrast_mul", 1.2, 100)


class RotationAndManifestTests(unittest.TestCase):
    def assertVector(self, actual, expected):
        for a, b in zip(actual, expected):
            self.assertAlmostEqual(a, b)

    def test_positive_rotation_is_counterclockwise(self):
        self.assertVector(core.rotate_z((1, 0, 2), 90), (0, 1, 2))
        self.assertVector(core.rotate_z((1, 0, 2), -90), (0, -1, 2))

    def test_transform_rotates_position_and_orientation_around_offset_pivot(self):
        rows = ((2, 0, 0), (0, 3, 0), (0, 0, 4), (11, 20, 7))
        actual = core.rotate_transform_z(rows, (10, 20, 5), 90)
        for a, b in zip(actual, ((0, 2, 0), (-3, 0, 0), (0, 0, 4), (10, 21, 7))):
            self.assertVector(a, b)
        restored = core.rotate_transform_z(actual, (10, 20, 5), -90)
        for a, b in zip(restored, rows):
            self.assertVector(a, b)

    def test_rotation_rejects_invalid_input(self):
        with self.assertRaises(ValueError):
            core.rotation_z_matrix(math.inf)
        with self.assertRaises(ValueError):
            core.rotate_transform_z(((1, 0, 0),), (0, 0, 0), 0)

    def test_material_kind_rules_and_precedence(self):
        for name, cls, bitmap, color, expected in (
            ("长虹", "CoronaPhysicalMtl", False, True, "glass"),
            ("", "GLASS", False, True, "glass"), ("玻璃五金", "", True, True, "glass"),
            ("银色五金", "", False, True, "metal"), ("金色", "", True, True, "metal"),
            ("", "Chrome", False, True, "metal"), ("摩卡红橡", "", True, True, "wood"),
            ("WALNUT", "", True, True, "wood"), ("胡桃木", "", False, True, "solid"),
            ("奶油白", "", False, True, "solid"), ("纹理", "", True, True, "other"),
            ("未知", "", False, False, "other")):
            with self.subTest(name=name, cls=cls):
                self.assertEqual(core.material_kind(name, cls, bitmap, color), expected)

    def test_palette_unique_separated_and_away_from_background(self):
        colors = core.region_palette(100)
        self.assertEqual(colors, core.region_palette(100))
        self.assertEqual(len(set(colors)), 100)
        for index, color in enumerate(colors):
            self.assertGreater(max(color) - min(color), 63)
            self.assertTrue(all(20 < c < 235 for c in color))
            for other in colors[:index]:
                self.assertGreater(max(abs(a-b) for a, b in zip(color, other)), 12)

    def test_palette_empty_invalid_and_over_capacity(self):
        self.assertEqual(core.region_palette(0), [])
        for count in (-1, True, 1.5, 10000):
            with self.subTest(count=count), self.assertRaises(ValueError):
                core.region_palette(count)

    def test_round_directory_default_and_validation(self):
        self.assertEqual(core.calibration_directory(Path("任务")), Path("任务/calibration/round-0"))
        self.assertEqual(core.calibration_directory(Path("任务"), 3), Path("任务/calibration/round-3"))
        for value in (-1, True, "../1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.calibration_directory(Path("任务"), value)


class CalibrationRunnerTests(unittest.TestCase):
    setUp = existing.RunnerContractTests.setUp
    prepare_masks = existing.RunnerContractTests.prepare_masks

    def prepare_material(self, name="胡桃木", class_name="CoronaMtl", **props):
        material = types.SimpleNamespace(name=name, class_name=class_name, **props)
        self.runtime.classOf.side_effect = lambda obj: obj.class_name
        self.runtime.getHandleByAnim.side_effect = id
        self.runtime.getPropNames.side_effect = lambda obj: list(vars(obj))
        self.runtime.getNumSubTexmaps.return_value = 0
        self.runtime.Color.side_effect = lambda r, g, b: types.SimpleNamespace(r=r, g=g, b=b)
        self.runner._calibration_unsupported = []
        return material

    def test_multimaterial_region_uses_most_faces_and_real_ids(self):
        wood = self.prepare_material(colorDiffuse=types.SimpleNamespace(r=100, g=80, b=60))
        other = types.SimpleNamespace(name='奶油白', class_name='Corona传统材质', colorDiffuse=wood.colorDiffuse)
        multi = types.SimpleNamespace(name='多维', class_name='多维材质', materialList=[other, wood], materialIDList=[8, 3])
        self.runtime.snapshotAsMesh.return_value = object()
        self.runtime.getNumFaces.return_value = 4
        self.runtime.getFaceMatID.side_effect = lambda mesh, i: [8, 3, 3, 3][i-1]
        nodes = [types.SimpleNamespace(name='板1', material=multi), types.SimpleNamespace(name='板2', material=wood)]
        materials, mains = self.runner._product_materials(nodes)
        self.assertEqual(materials[id(wood)]['objects'], 2)
        self.assertEqual(len(materials), 2)
        self.assertIs(mains[0][1], wood)
        self.assertTrue(any('面数最多' in message for message in self.runner._calibration_unsupported))

    def test_texture_wraps_original_once_for_shared_material(self):
        texture = types.SimpleNamespace(class_name="Bitmaptexture", filename="D:/贴图/oak.jpg")
        wood = self.prepare_material(texmapDiffuse=texture)
        correction = types.SimpleNamespace(inputMap=None, saturation=0.0, brightness=1.0, contrast=1.0, hueShift=0.0)
        self.runtime.CoronaColorCorrect.return_value = correction
        self.runtime.getNumSubMtls.return_value = 0
        nodes = [types.SimpleNamespace(name="板1", material=wood), types.SimpleNamespace(name="板2", material=wood)]
        self.runner.job["overrides"] = {"materials": {"胡桃木": {"saturation_mul": 1.4, "brightness_mul": 1.2}}}
        self.runner._apply_calibration(nodes, [], (0, 0, 0), 1)
        self.runtime.CoronaColorCorrect.assert_called_once()
        self.assertIs(wood.texmapDiffuse, correction)
        self.assertIs(correction.inputMap, texture)
        self.assertAlmostEqual(correction.saturation, 0.4)
        self.assertEqual(correction.brightness, 1.2)

    def test_plain_color_hsv_and_non_glass_properties_skipped(self):
        color = types.SimpleNamespace(r=200, g=100, b=100)
        wood = self.prepare_material(colorDiffuse=color, levelRefract=0.5, refractIor=1.5, opacity=1.0)
        self.runner._apply_material(wood, {"brightness_mul": 0.6, "refraction_mul": 0, "ior": 1.8, "opacity_mul": 0.5})
        self.assertAlmostEqual(wood.colorDiffuse.r, 120)
        self.assertEqual((wood.levelRefract, wood.refractIor, wood.opacity), (0.5, 1.5, 1))
        self.runtime.CoronaColorCorrect.assert_not_called()

    def test_repeated_color_adjustment_replaces_own_layer(self):
        texture = types.SimpleNamespace(class_name="Bitmaptexture", filename="oak.jpg")
        wood = self.prepare_material(texmapDiffuse=texture)
        first = types.SimpleNamespace(class_name="CoronaColorCorrect", inputMap=None, brightness=1.0)
        second = types.SimpleNamespace(class_name="CoronaColorCorrect", inputMap=None, brightness=1.0)
        self.runtime.CoronaColorCorrect.side_effect = [first, second]
        self.runner._apply_material(wood, {"brightness_mul": 1.2})
        self.runner._apply_material(wood, {"brightness_mul": 1.4})
        self.assertIs(wood.texmapDiffuse, second)
        self.assertIs(second.inputMap, texture)
        self.assertEqual(second.brightness, 1.4)

    def test_light_targets_are_deduplicated_and_parent_first(self):
        self.prepare_material()
        parent = types.SimpleNamespace(name="父灯", parent=None, transform="父矩阵")
        target = types.SimpleNamespace(name="目标", parent=parent, transform="目标矩阵")
        child = types.SimpleNamespace(name="子灯", parent=parent, target=target, transform="子矩阵")
        parent.target = target
        self.runtime.copy.side_effect = lambda value: value
        snapshots = self.runner._light_transforms([child, parent])
        self.assertEqual(len(snapshots), 3)
        self.assertIs(snapshots[0][0], parent)
        self.assertEqual({matrix for _, matrix in snapshots}, {"父矩阵", "子矩阵", "目标矩阵"})

    def test_smallshot_renders_regions_in_same_render_and_restores_paths(self):
        manager, item = self.prepare_masks()
        self.runner.job.update(mode="smallshot", want_manifest=True)
        item.update(width=600, height=800)
        self.runner._camera = mock.Mock(return_value=(object(), object()))
        self.runner._touch = mock.Mock()
        self.runtime.render.return_value = (types.SimpleNamespace(filename=""), [types.SimpleNamespace(filename=""), None])
        self.runner._save_mask = mock.Mock(return_value="")
        with mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(Path, "stat", return_value=types.SimpleNamespace(st_size=1)):
            self.assertEqual(self.runner._render_item(item, (0, 0, 0), 1), "")
        self.runtime.render.assert_called_once()
        self.assertIn("renderElementBitmaps", self.runtime.render.call_args.kwargs)
        self.assertEqual(manager.SetRenderElementFilename.call_args_list[-2:],
                         [mock.call(0, "原路径0.jpg"), mock.call(1, "原路径1.jpg")])

    def test_missing_wire_still_finishes_smallshot(self):
        manager, item = self.prepare_masks()
        manager.NumRenderElements.return_value = 0
        self.runner.job.update(mode="smallshot", want_manifest=True)
        item.update(width=600, height=800)
        self.runner._camera = mock.Mock(return_value=(object(), object()))
        self.runner._touch = mock.Mock()
        self.runtime.render.return_value = (types.SimpleNamespace(filename=""), [])
        with mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(Path, "stat", return_value=types.SimpleNamespace(st_size=1)):
            reason = self.runner._render_item(item, (0, 0, 0), 1)
        self.assertIn("没有找到遮罩_ID", reason)
        self.runtime.render.assert_called_once()

    def test_legacy_and_physical_glass_properties(self):
        for cls, props, expected in (
            ("CoronaLegacyMtl", {"levelRefract": 0.8, "refractIor": 1.5, "opacity": 1.0, "reflectGlossiness": 0.5},
             {"levelRefract": 0.4, "refractIor": 1.8, "opacity": 0.5, "reflectGlossiness": 0.7}),
            ("CoronaPhysicalMtl", {"refractionAmount": 0.8, "baseIor": 1.5, "opacityLevel": 1.0, "baseRoughness": 0.5},
             {"refractionAmount": 0.4, "baseIor": 1.8, "opacityLevel": 0.5, "baseRoughness": 0.3})):
            with self.subTest(cls=cls):
                glass = self.prepare_material("长虹玻璃", cls, **props)
                self.runner._apply_material(glass, {"refraction_mul": 0.5, "ior": 1.8, "opacity_mul": 0.5, "glossiness_delta": 0.2})
                for prop, value in expected.items():
                    self.assertAlmostEqual(getattr(glass, prop), value)

    def test_missing_property_logs_and_does_not_write_guessed_property(self):
        wood = self.prepare_material()
        self.runner._apply_material(wood, {"reflection_mul": 1.5})
        self.assertIn("胡桃木：没有可调的 reflection_mul", self.runner._calibration_unsupported)
        self.runtime.setProperty.assert_not_called()

    def test_environment_wrapper_and_combined_light_multiplier(self):
        self.prepare_material()
        texture = types.SimpleNamespace(name="环境")
        renderer = types.SimpleNamespace(bg_texmapReflect=texture, progressive_passlimit=7, bg_overrideReflect=True)
        self.runtime.renderers.current = renderer
        correction = types.SimpleNamespace(inputMap=None, brightness=0.0)
        self.runtime.CoronaColorCorrect.return_value = correction
        light = types.SimpleNamespace(name="灯", intensity=10)
        self.runner.job["overrides"] = {"lights": {"intensity_mul": 8, "per_light": {"灯": {"intensity_mul": 9}}},
                                        "environment": {"reflection_strength_mul": 2}}
        self.runner._apply_calibration([], [light], (0, 0, 0), 1)
        self.assertEqual(light.intensity, 10)
        with self.runner._item_lights({"no": 1}):
            self.assertEqual(light.intensity, 32)
        self.assertEqual(light.intensity, 10)
        self.assertIs(renderer.bg_texmapReflect.inputMap, texture)
        self.assertEqual(correction.brightness, 1)
        self.assertEqual((renderer.progressive_passlimit, renderer.bg_overrideReflect), (7, True))

    def test_item_angle_replaces_global_and_restores_after_exception(self):
        self.runner._overrides = core.clamp_overrides({"lights": {"rotate_deg": 60}, "items": {"1": {"lights": {"rotate_deg": -30}}}})
        original = object()
        node = types.SimpleNamespace(transform=original)
        self.runner._template_lights = [node]
        self.runner._light_center = (10, 20, 0)
        self.runner._light_transforms = mock.Mock(return_value=[(node, original)])
        self.runner._rotate_transforms = mock.Mock(side_effect=lambda *args: setattr(node, "transform", "旋转后"))
        with self.assertRaises(RuntimeError):
            with self.runner._item_lights({"no": 1}):
                self.assertEqual(node.transform, "旋转后")
                raise RuntimeError("模拟渲染异常")
        self.assertIs(node.transform, original)
        self.runner._rotate_transforms.assert_called_once_with([(node, original)], (10, 20, 0), -30)
        with self.runner._item_lights({"no": 2}):
            pass
        self.assertEqual(self.runner._rotate_transforms.call_count, 2)
        self.assertEqual(self.runner._rotate_transforms.call_args.args[-1], 60)

    def test_light_restore_failure_stops_job(self):
        self.runner._overrides = core.clamp_overrides({"items": {"1": {"lights": {"rotate_deg": 30}}}})
        class BrokenNode:
            @property
            def transform(self):
                return None
            @transform.setter
            def transform(self, value):
                raise RuntimeError("控制器拒绝")
        self.runner._template_lights = []
        self.runner._light_center = (0, 0, 0)
        self.runner._light_transforms = mock.Mock(return_value=[(BrokenNode(), None)])
        self.runner._rotate_transforms = mock.Mock()
        with self.assertRaises(self.module.SceneRestoreError):
            with self.runner._item_lights({"no": 1}):
                pass

    def test_calibration_wire_route_is_png_at_requested_round(self):
        manager, item = self.prepare_masks()
        self.runner.job.update(want_manifest=True, calibration_round=2, mode="smallshot")
        paths = []
        routes = self.runner._mask_setup(item, paths)["routes"]
        wire = next(route for route in routes if route["index"] == 0)
        self.assertEqual(Path(wire["path"]), self.runner.exchange / "tasks/任务1/calibration/round-2/regions/3.png")
        self.assertIn(mock.call(wire["path"], ".png"), self.runner._output.call_args_list)
        manager.SetElementsActive.assert_not_called()

    def test_missing_wire_channel_does_not_abort(self):
        manager, item = self.prepare_masks()
        self.runner.job["want_manifest"] = True
        manager.NumRenderElements.return_value = 0
        self.assertEqual(self.runner._mask_setup(item, [])["routes"], [])
        self.assertTrue(any("没有精确匹配到遮罩_线框颜色" in str(call) for call in self.runner.log.call_args_list))

    def test_manifest_and_legend_written_with_actual_properties(self):
        wood = self.prepare_material(colorDiffuse=types.SimpleNamespace(r=180, g=120, b=60), levelReflect=0.6)
        nodes = [types.SimpleNamespace(name="板1", material=wood), types.SimpleNamespace(name="板2", material=wood)]
        self.runtime.getNumSubMtls.return_value = 0
        self.runtime.renderers.current = types.SimpleNamespace(bg_texmapReflect=None)
        self.runner.exchange = existing.TMP_DIR / ("manifest-" + uuid4().hex[:8])
        self.runner.job.update(want_manifest=True, calibration_round=4)
        self.runner._bbox = mock.Mock(return_value=((0, 0, 0), (2, 4, 6)))
        self.runner._apply_calibration(nodes, [], (1, 2, 0), 10)
        directory = self.runner._calibration_dir()
        manifest = core.read_json(directory / "scene_manifest.json")
        self.assertEqual(set(manifest), {"materials", "lights", "environment", "product_center_mm", "unsupported"})
        self.assertEqual(manifest["product_center_mm"], [10, 20, 30])
        self.assertEqual(manifest["materials"][0], {"name": "胡桃木", "class": "CoronaMtl", "color": [180, 120, 60],
                         "bitmap": None, "params": {"colorDiffuse": [180, 120, 60], "levelReflect": 0.6}, "kind": "solid", "objects": 2})
        legend = core.read_json(directory / "regions/legend.json")
        self.assertEqual(list(legend.values()), ["胡桃木"])
        self.assertEqual(vars(nodes[0].wirecolor), vars(nodes[1].wirecolor))
        self.runner._apply_calibration(nodes, [], (1, 2, 0), 10)
        self.assertEqual(list(directory.rglob("*.bak")), [])


if __name__ == "__main__":
    unittest.main()
