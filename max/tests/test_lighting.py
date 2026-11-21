"""§12 打光的计算和模拟调用契约；不能代替 Max/Corona 实机验收。"""

import ast
from contextlib import nullcontext
import math
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wb_core as core
import test_wb_core as existing


LEGACY_TUNING = dict(fill_min=.25, fill_gamma=1, env_gi_min=1, key_size_max=.5, key_size_min=.5,
                     key_distance=2.5, key_compensation=0, bg_fill_keep=0)


class LightingMathTests(unittest.TestCase):
    def test_null_and_missing_azimuth_disable_lighting(self):
        self.assertIsNone(core.normalize_lighting(None))
        for value in ({}, {"azimuth_deg": None}, [], "无效"):
            self.assertIsNone(core.normalize_lighting(value)["azimuth_deg"])

    def test_invalid_azimuth_is_null(self):
        for value in (True, False, "45", math.nan, math.inf, -math.inf, [], {}):
            with self.subTest(value=value):
                self.assertIsNone(core.normalize_lighting({"azimuth_deg": value})["azimuth_deg"])

    def test_defaults_and_zero_azimuth(self):
        self.assertEqual(core.normalize_lighting({"azimuth_deg": 0}),
                         {"azimuth_deg": 0, "elevation_deg": 35, "intensity": 1, "contrast": 0.5})

    def test_all_ranges_clamped(self):
        for key, (low, high) in core.LIGHTING_LIMITS.items():
            for supplied, expected in ((-1000, low), (1000, high), ((low + high) / 2, (low + high) / 2)):
                with self.subTest(key=key, value=supplied):
                    self.assertEqual(core.normalize_lighting({key: supplied})[key], expected)

    def test_invalid_non_direction_fields_default(self):
        for key in ("elevation_deg", "intensity", "contrast"):
            for value in (True, "1", None, math.nan, math.inf):
                with self.subTest(key=key, value=value):
                    self.assertEqual(core.normalize_lighting({key: value})[key], core.LIGHTING_DEFAULTS[key])

    def test_normalization_does_not_mutate_input(self):
        source = {"azimuth_deg": 900, "contrast": -2, "extra": 1}
        result = core.normalize_lighting(source)
        self.assertEqual(source, {"azimuth_deg": 900, "contrast": -2, "extra": 1})
        self.assertNotIn("extra", result)

    def test_tuning_curves_and_endpoints(self):
        tuning = core.normalize_lighting_tuning(dict(fill_min=.1, fill_gamma=2, env_gi_min=.2,
            key_size_min=.2, key_size_max=.6, key_distance=1.8, key_intensity=14,
            key_compensation=.5, bg_fill_keep=.6))
        for contrast, fill, size, env in ((0, 1, .6, 1), (.5, .775, .4, .6), (1, .1, .2, .2)):
            with self.subTest(contrast=contrast):
                light = core.normalize_lighting({"contrast": contrast, "intensity": 1.2})
                result = core.lighting_response(light, tuning, 12, 1.1)
                self.assertAlmostEqual(result["fill_ratio"], fill)
                self.assertAlmostEqual(result["bg_fill_ratio"], fill + (1 - fill) * .6)
                self.assertAlmostEqual(result["env_gi"], env)
                self.assertAlmostEqual(result["key_size"], size)
                self.assertEqual(result["key_distance"], 1.8)
                self.assertAlmostEqual(result["key_strength"], 14 * 1.2 * 1.1 * (1 + .5 * (1 - fill)))

    def test_invalid_tuning_fields_fall_back_independently(self):
        self.assertEqual(core.normalize_lighting_tuning({
            "fill_min": True, "fill_gamma": 0, "env_gi_min": -1, "key_distance": "2.5",
            "key_size_min": 1, "key_size_max": .5, "key_intensity": math.inf,
            "bg_fill_keep": 2, "probe": 1, "env_gi_properties": {"unknown": "实验属性"},
        }), core.normalize_lighting_tuning(None))

    def test_fill_ratio_edges_midpoint_and_clamping(self):
        for value, expected in ((0, 1), (0.5, 0.625), (1, 0.25), (-1, 1), (2, 0.25)):
            self.assertEqual(core.lighting_fill_ratio(value, .25, 1), expected)
        for value, contrast in ((-1, 0), (0, 0), (.25, .25), (.5, .5), (1, 1), (2, 1)):
            self.assertEqual(core.lighting_fill_ratio(value), 1 - .9 * contrast)

    def test_cardinal_pf1_directions_map_to_max(self):
        horizontal = 10 * math.cos(math.radians(35))
        for az, dx, dy in ((0, 0, -horizontal), (90, horizontal, 0),
                           (-90, -horizontal, 0), (180, 0, horizontal), (-180, 0, horizontal)):
            with self.subTest(az=az):
                position = core.lighting_position_max(az, 35, 10, (11, 22, 33))
                for actual, expected in zip(position, (11 + dx, 22 + dy, 33 + 10 * math.sin(math.radians(35)))):
                    self.assertAlmostEqual(actual, expected)

    def test_position_distance_and_pf1_roundtrip_all_units(self):
        for scale in (1, 10, 1000):
            center = (10 / scale, -30 / scale, 50 / scale)
            distance = 2500 / scale
            position = core.lighting_position_max(-45, 70, distance, center)
            self.assertAlmostEqual(math.dist(position, center), distance)
            pf1 = core.max_to_pf1(position, center, scale)
            self.assertLess(pf1[0], 0)
            self.assertGreater(pf1[1], 0)
            self.assertGreater(pf1[2], 0)
            self.assertAlmostEqual(math.degrees(math.atan2(pf1[0], pf1[2])), -45)
            self.assertAlmostEqual(math.degrees(math.atan2(pf1[1], math.hypot(pf1[0], pf1[2]))), 70)

    def test_invalid_distance_and_center_rejected(self):
        for value in (0, -1, math.nan, math.inf, True, "10"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.lighting_position_max(0, 35, value)
        with self.assertRaises(ValueError):
            core.lighting_position_max(0, 35, 1, (math.nan, 0, 0))

    def test_corona_class_names_english_chinese_and_fuzzy(self):
        for name in ("CoronaLight", "Corona灯光", "CR灯光", "coronalight", "_Corona矩形灯", "CR Light"):
            with self.subTest(name=name):
                self.assertTrue(core.is_corona_light_class_name(name))
        for name in ("OmniLight", "CoronaMtl", "CR材质", "灯光", "", "ScriptedLight"):
            with self.subTest(name=name):
                self.assertFalse(core.is_corona_light_class_name(name))

    def test_description_direction_height_and_contrast(self):
        self.assertEqual(core.lighting_description(None), "模板默认灯光")
        for az, el, contrast, tokens in (
            (-45, 35, 0.5, ("左前方", "斜上方", "左侧受光较亮", "中等")),
            (90, 10, 1, ("右侧", "低角度", "强烈")),
            (0, 70, 0, ("正前方", "高处", "顶面最亮", "柔和")),
            (-150, 35, 0.5, ("左后方（逆光）",))):
            text = core.lighting_description({"azimuth_deg": az, "elevation_deg": el,
                                              "contrast": contrast})
            for token in tokens:
                self.assertIn(token, text)


class Node:
    def __init__(self, **values):
        self.valid = True
        self.__dict__.update(values)


class LampClass:
    def __init__(self, name, factory):
        self.name, self.factory = name, factory

    def __str__(self):
        return self.name

    def __call__(self):
        return self.factory()


class LightingRunnerTests(unittest.TestCase):
    setUp = existing.RunnerContractTests.setUp

    def prepare(self, *, auto_target=False):
        self.runner._calibration_unsupported = []
        self.runner._scene_manifest = None
        self.runner._overrides = core.clamp_overrides(None)
        self.runner.config["key_light_intensity"] = 20
        self.runner.job["lighting_tuning"] = dict(LEGACY_TUNING)
        self.lamps = [Node(name="模板主灯", class_name="CR灯光", intensity=10,
                           colorMode=1, blackbodyTemp=6500, color=(255, 250, 245), pos=(5, 6, 7)),
                      Node(name="模板辅灯", class_name="OmniLight", multiplier=4, pos=(9, 8, 7))]
        self.runner._template_lights = self.lamps
        self.created = []

        def create():
            node = Node(name="", class_name="CR灯光", intensity=0, shape=1, width=1, height=1,
                        visibleDirectly=True, visibleRefl=True, visibleRefract=True, targeted=False,
                        on=False, colorMode=0, blackbodyTemp=3000, color=(0, 0, 0),
                        target=Node(pos=None) if auto_target else None, pos=None)
            self.created.append(node)
            return node

        self.runtime.light.classes = [LampClass("CR灯光", create)]
        self.runtime.Point3.side_effect = lambda *values: tuple(values)
        self.runtime.TargetObject.side_effect = lambda: Node(pos=None)
        self.runtime.isValidNode.side_effect = lambda node: node.valid
        self.runtime.delete.side_effect = lambda node: setattr(node, "valid", False)
        self.runtime.classOf.side_effect = lambda node: node.class_name
        self.runner._light_transforms = mock.Mock()
        self.runner._rotate_transforms = mock.Mock()
        self.bounds = ((10, -20, 30), (13, -16, 42))  # 对角线 13，中心 (11.5,-18,36)。
        self.item = {"no": 1, "lighting": {"azimuth_deg": 45}}

    def test_key_light_geometry_visibility_color_and_cleanup(self):
        self.prepare()
        with self.runner._item_lights(self.item, self.bounds, 10):
            key = self.created[0]
            target = key.target
            self.assertEqual((key.shape, key.width, key.height), (0, 6.5, 6.5))
            self.assertEqual(target.pos, (11.5, -18, 36))
            self.assertAlmostEqual(math.dist(key.pos, target.pos), 32.5)
            self.assertEqual(key.pos, core.lighting_position_max(45, 35, 32.5, target.pos))
            self.assertTrue(key.targeted and key.on)
            self.assertFalse(key.visibleDirectly or key.visibleRefl or key.visibleRefract)
            self.assertEqual((key.colorMode, key.blackbodyTemp, key.color), (1, 6500, (255, 250, 245)))
            self.assertEqual(key.intensity, 20)
            self.assertEqual((self.lamps[0].intensity, self.lamps[1].multiplier), (6.25, 2.5))
            self.assertEqual([light.pos for light in self.lamps], [(5, 6, 7), (9, 8, 7)])
        self.assertFalse(key.valid or target.valid)
        self.assertEqual((self.lamps[0].intensity, self.lamps[1].multiplier), (10, 4))
        self.runtime.CoronaLight.assert_not_called()

    def test_explicit_legacy_tuning_matches_previous_runner_at_each_contrast(self):
        self.prepare()
        self.runner.config["key_light_intensity"] = 12
        for contrast in (0, .23, .5, 1):
            self.item["lighting"].update(contrast=contrast, intensity=1.2)
            with self.runner._item_lights(self.item, self.bounds):
                key = self.created[-1]
                old_distance = 2.5 * 13
                self.assertEqual(self.lamps[0].intensity, 10 * ((1 - .75 * contrast) * 1.2))
                self.assertEqual(key.width, .5 * 13)
                self.assertEqual(key.height, .5 * 13)
                self.assertEqual(key.pos, core.lighting_position_max(45, 35, old_distance, key.target.pos))
                self.assertAlmostEqual(math.dist(key.pos, key.target.pos), old_distance)
                self.assertEqual(key.intensity, 12 * 1.2)

    def test_missing_tuning_uses_measured_defaults_and_unlit_keeps_template(self):
        self.prepare()
        self.runner.config.pop("key_light_intensity")
        self.runner.job.pop("lighting_tuning")
        self.item["lighting"]["contrast"] = 1
        with self.runner._item_lights(self.item, self.bounds):
            key = self.created[-1]
            self.assertAlmostEqual(self.lamps[0].intensity, 10 * .10)
            self.assertAlmostEqual(key.width, 13 * .25)
            self.assertAlmostEqual(math.dist(key.pos, key.target.pos), 13 * 2.5)
            self.assertAlmostEqual(key.intensity, 100.9)
        self.runtime.setProperty.reset_mock()
        with self.runner._item_lights({"no": 1, "lighting": {"azimuth_deg": None}}, self.bounds):
            self.assertEqual((self.lamps[0].intensity, self.lamps[1].multiplier), (10, 4))
        self.runtime.setProperty.assert_not_called()

    def test_job_tuning_wins_and_keeps_only_lamp_aimed_at_floor(self):
        self.prepare()
        self.runner.config.update(key_light_intensity=99, lighting_tuning={"key_intensity": 88})
        self.runner.job.update(key_light_intensity=12, lighting_tuning={
            "fill_min": .2, "key_distance": 1.5, "key_size_max": .6, "key_size_min": .2,
            "bg_fill_keep": .75, "key_compensation": .5,
        })
        self.runner._lighting_background_boxes = {
            "Rectangle2139486305": ((0, -40, 0), (100, 50, 0))}
        self.lamps[0].pos, self.lamps[0].target = (11, -18, 50), Node(pos=(11, -18, 0))
        self.lamps[1].pos, self.lamps[1].target = (60, -18, 50), Node(pos=(60, -18, 0))
        self.item["lighting"]["contrast"] = 1
        with self.runner._item_lights(self.item, self.bounds):
            self.assertAlmostEqual(self.lamps[0].intensity, 2)
            self.assertAlmostEqual(self.lamps[1].multiplier, 3.2)
            key = self.created[0]
            self.assertAlmostEqual(key.width, 2.6)
            self.assertAlmostEqual(math.dist(key.pos, key.target.pos), 19.5)
            self.assertAlmostEqual(key.intensity, 16.8)
        self.assertEqual((self.lamps[0].intensity, self.lamps[1].multiplier), (10, 4))
        self.assertIn("Rectangle2139486305", str(self.runner.log.call_args_list))

    def prepare_environment(self, **values):
        self.runner.job["lighting_tuning"] = {**LEGACY_TUNING, "env_gi_min": .2, "env_gi_properties": {
            "gi_multiplier": "test_gi", "direct_enabled": "test_direct",
            "direct_map": "test_map", "direct_color": "test_color",
        }}
        self.item["lighting"]["contrast"] = .5
        renderer = Node(class_name="CoronaRenderer", test_gi=2.0, test_direct=False,
                        test_map="模板备用贴图", test_color=(7, 8, 9))
        renderer.__dict__.update(values)
        self.runtime.renderers.current = renderer
        self.runtime.useEnvironmentMap = False
        self.runtime.backgroundColor = (255, 254, 253)
        return renderer

    def test_environment_background_is_copied_and_scene_restored_on_render_exception(self):
        self.prepare()
        renderer = self.prepare_environment()
        original = vars(renderer).copy()
        with self.assertRaisesRegex(RuntimeError, "模拟渲染失败"):
            with self.runner._item_lights(self.item, self.bounds):
                self.assertTrue(renderer.test_direct)
                self.assertEqual(renderer.test_color, self.runtime.backgroundColor)
                self.assertAlmostEqual(renderer.test_gi, 1.2)
                raise RuntimeError("模拟渲染失败")
        self.assertEqual(vars(renderer), original)

    def test_environment_preserves_existing_override_and_copies_scene_map_when_needed(self):
        self.prepare()
        renderer = self.prepare_environment(test_direct=True)
        original = vars(renderer).copy()
        self.runtime.useEnvironmentMap = True
        self.runtime.environmentMap = object()
        with self.runner._item_lights(self.item, self.bounds):
            self.assertEqual(renderer.test_map, original["test_map"])
            self.assertAlmostEqual(renderer.test_gi, 1.2)
        self.assertEqual(vars(renderer), original)
        renderer.test_direct = False
        original = vars(renderer).copy()
        with self.runner._item_lights(self.item, self.bounds):
            self.assertIs(renderer.test_map, self.runtime.environmentMap)
            self.assertTrue(renderer.test_direct)
        self.assertEqual(vars(renderer), original)

    def test_environment_can_copy_the_renderer_owned_source_map(self):
        self.prepare()
        renderer = self.prepare_environment(source=object())
        original = vars(renderer).copy()
        self.runner.job["lighting_tuning"]["env_gi_properties"]["source_map"] = "source"
        self.runtime.useEnvironmentMap = True
        self.runtime.environmentMap = object()
        with self.runner._item_lights(self.item, self.bounds):
            self.assertIs(renderer.test_map, renderer.source)
            self.assertTrue(renderer.test_direct)
        self.assertEqual(vars(renderer), original)

    def test_environment_ignores_silent_background_setter_failure(self):
        self.prepare()
        renderer = self.prepare_environment()
        original = vars(renderer).copy()

        def set_property(obj, prop, value):
            if obj is not renderer or prop != "test_color":
                setattr(obj, prop, value)

        self.runtime.setProperty.side_effect = set_property
        with self.runner._item_lights(self.item, self.bounds):
            self.assertEqual(vars(renderer), original)
            self.assertEqual(self.lamps[0].intensity, 6.25)
        self.assertEqual(vars(renderer), original)
        self.assertIn("未保留场景背景值", str(self.runner.log.call_args_list))

    def test_missing_environment_property_skips_only_gi_without_mutation(self):
        self.prepare()
        renderer = self.prepare_environment()
        for missing in ("test_color", "test_direct", "test_gi"):
            with self.subTest(missing=missing):
                old = vars(renderer).pop(missing)
                original = vars(renderer).copy()
                with self.runner._item_lights(self.item, self.bounds):
                    self.assertEqual(vars(renderer), original)
                    self.assertEqual(self.lamps[0].intensity, 6.25)
                self.assertEqual(vars(renderer), original)
                setattr(renderer, missing, old)
        self.assertIn("已跳过 Corona 环境 GI", str(self.runner.log.call_args_list))

    def test_partial_environment_setup_is_rolled_back_before_render(self):
        self.prepare()
        renderer = self.prepare_environment()
        original = vars(renderer).copy()

        def set_property(obj, prop, value):
            setattr(obj, prop, value)
            if obj is renderer and prop == "test_gi" and value != original[prop]:
                raise RuntimeError("模拟 GI 设置失败")

        self.runtime.setProperty.side_effect = set_property
        with self.runner._item_lights(self.item, self.bounds):
            self.assertEqual(vars(renderer), original)
            self.assertEqual(self.lamps[0].intensity, 6.25)
        self.assertEqual(vars(renderer), original)

    def test_probe_uses_enumerated_properties_and_logs_light_pose_and_background_boxes(self):
        self.prepare()
        renderer = self.prepare_environment(bg_test="白色", gi_experimental=1, exposure=0, unrelated=42)
        self.runner.job.update(job_id="测试作业", max_file="测试.max", items=[self.item])
        self.item["camera"] = {"frame": "PF1", "position_mm": [0, 0, 10], "target_mm": [0, 0, 0]}
        self.runner.job["lighting_tuning"]["probe"] = True
        self.runner.status = {"items": [{}]}
        self.runner.config["template_max"] = "模板.max"
        self.runner.job["template_anchor"] = {"x_mm": 11.5, "back_y_mm": -16, "floor_z_mm": 30}
        floor = Node(name="Rectangle2139486305", class_name="Plane")
        self.lamps[0].target = Node(pos=(5, 6, 0))
        self.lamps[0].width = 20
        self.runtime.objects = [*self.lamps, floor]
        self.runtime.superClassOf.side_effect = lambda node: self.runtime.Light if node in self.lamps else None
        self.runtime.getPropNames.side_effect = lambda obj: list(vars(obj))
        self.runner._load = self.runner._remove_template_cameras = self.runner._touch = mock.Mock()
        self.runner._merge = mock.Mock(return_value=[])
        self.runner._geometry = mock.Mock(return_value=[])
        self.runner._move_roots = self.runner._material_log = mock.Mock()
        floor_bounds = ((-50, -50, 0), (50, 50, 0))
        self.runner._bbox = mock.Mock(side_effect=[floor_bounds, self.bounds, self.bounds])
        self.runner._mm_per_unit = mock.Mock(return_value=1)
        self.runner._smallshot_time = nullcontext
        self.runner._render_item = mock.Mock(return_value="")
        self.runner.log_file = mock.Mock()
        with mock.patch.object(self.runner, "_item_lights", return_value=nullcontext()):
            self.runner.render_job()
        logs = "\n".join(call.args[0] for call in self.runner.log_file.call_args_list)
        for expected in ("测试作业", "bg_test=白色", "gi_experimental=1", "exposure=0", "CR灯光",
                         "'width': '20'", "'intensity': '10'", "(5, 6, 7)", "(0.0, 0.0, -1.0)",
                         "Rectangle2139486305 世界包围盒 " + str(floor_bounds)):
            self.assertIn(expected, logs)
        self.assertNotIn("unrelated", logs)
        self.assertEqual(renderer.unrelated, 42)

    def test_sweep_smallshot_resaves_stale_beauty_and_id_from_same_render(self):
        self.prepare()
        manager, item = existing.RunnerContractTests.prepare_masks(self)
        self.runner.job.update(mode="smallshot", lighting_tuning={"capture_mask": True})
        item.update(width=600, height=800)
        self.runner._camera = mock.Mock(return_value=(Node(), Node()))
        self.runner._touch = mock.Mock()
        self.runner._check_not_blank = mock.Mock()
        beauty, bitmap = Node(filename=""), Node(filename="")
        self.runtime.render.return_value = (beauty, [None, bitmap])
        original = (1, 2, 100)
        stamps = {item["out_image"]: original, item["out_mask"]: original}
        self.runtime.save.side_effect = lambda image: stamps.update({image.filename: (3, 4, 200)})
        with mock.patch.object(core, "lighting_output_stamp", side_effect=stamps.get):
            self.assertEqual(self.runner._render_item(item, (0, 0, 0), 1),
                             "遮罩_线框颜色：renderElementBitmaps 对应遮罩位图为空")
        self.assertEqual(self.runtime.save.call_args_list, [mock.call(beauty), mock.call(bitmap)])
        self.runtime.render.assert_called_once()
        self.assertIn("renderElementBitmaps", self.runtime.render.call_args.kwargs)
        self.assertEqual(manager.SetRenderElementFilename.call_args_list[-2:],
                         [mock.call(0, "原路径0.jpg"), mock.call(1, "原路径1.jpg")])

    def test_ai_multiplier_once_clamped_no_rotation_or_per_light(self):
        self.prepare()
        self.runner._overrides = core.clamp_overrides({"lights": {"intensity_mul": 1.6, "rotate_deg": 90,
                  "per_light": {"模板主灯": {"intensity_mul": 2}}}, "items": {"1": {"lights": {"rotate_deg": -90}}}})
        self.runner.config["key_light_intensity"] = 30
        self.item["lighting"].update(intensity=1.2, contrast=1)
        with self.runner._item_lights(self.item, self.bounds):
            self.assertAlmostEqual(self.created[0].intensity, 30 * 1.2 * 1.15)
            self.assertAlmostEqual(self.lamps[0].intensity, 10 * 0.25 * 1.2 * 1.15)
            self.assertAlmostEqual(self.lamps[1].multiplier, 4 * 0.25 * 1.2 * 1.15)
        self.runner._light_transforms.assert_not_called()
        self.runner._rotate_transforms.assert_not_called()

    def test_ai_lower_clamp(self):
        self.prepare()
        self.runner._overrides["lights"]["intensity_mul"] = 0.5
        with self.runner._item_lights(self.item, self.bounds):
            self.assertAlmostEqual(self.created[0].intensity, 17)

    def test_null_missing_and_invalid_azimuth_are_no_op(self):
        self.prepare()
        for lighting in (None, {}, {"azimuth_deg": None}, {"azimuth_deg": math.nan}):
            with self.subTest(lighting=lighting):
                with self.runner._item_lights({"no": 1, "lighting": lighting}, self.bounds):
                    self.assertEqual(self.lamps[0].intensity, 10)
        self.assertFalse(self.created)
        self.runtime.setProperty.assert_not_called()
        self.runtime.delete.assert_not_called()

    def test_each_item_starts_from_original_template(self):
        self.prepare(auto_target=True)
        for no, az, contrast, intensity in ((1, -45, 1, 0.6), (2, 90, 0, 1.5)):
            item = {"no": no, "lighting": {"azimuth_deg": az, "contrast": contrast, "intensity": intensity}}
            with self.runner._item_lights(item, self.bounds):
                self.assertAlmostEqual(self.lamps[0].intensity, 10 * (1 - 0.75 * contrast) * intensity)
                self.assertAlmostEqual(self.created[-1].intensity, 20 * intensity)
            self.assertEqual(self.lamps[0].intensity, 10)
        self.assertEqual(len(self.created), 2)
        self.runtime.TargetObject.assert_not_called()
        self.assertTrue(all(not light.valid and not light.target.valid for light in self.created))

    def test_render_exception_restores_all_lights_and_nodes(self):
        self.prepare()
        with self.assertRaisesRegex(RuntimeError, "模拟渲染异常"):
            with self.runner._item_lights(self.item, self.bounds):
                raise RuntimeError("模拟渲染异常")
        self.assertEqual((self.lamps[0].intensity, self.lamps[1].multiplier), (10, 4))
        self.assertFalse(self.created[0].valid or self.created[0].target.valid)

    def test_class_lookup_missing_logs_and_continues_without_changes(self):
        self.prepare()
        self.runtime.light.classes = [LampClass("OmniLight", lambda: self.fail("不应创建"))]
        with self.runner._item_lights(self.item, self.bounds):
            self.assertEqual(self.lamps[0].intensity, 10)
        self.assertTrue(any("找不到 Corona 灯类" in text for text in self.runner._calibration_unsupported))
        self.runtime.setProperty.assert_not_called()

    def test_class_enumeration_failure_continues(self):
        self.prepare()
        self.runtime.light.classes = None
        with self.runner._item_lights(self.item, self.bounds):
            pass
        self.assertTrue(any("无法枚举" in text for text in self.runner._calibration_unsupported))

    def test_exact_class_names_preferred_over_fuzzy(self):
        self.prepare()
        fallback = LampClass("_Corona矩形灯", lambda: None)
        exact = self.runtime.light.classes[0]
        self.runtime.light.classes = [fallback, exact]
        self.assertIs(self.runner._corona_light_class(), exact)
        self.runtime.light.classes = [fallback]
        self.assertIs(self.runner._corona_light_class(), fallback)

    def test_missing_required_property_skips_and_cleans_auto_target(self):
        self.prepare(auto_target=True)
        original = self.runtime.light.classes[0].factory

        def create():
            key = original()
            # 2026-11-20 实测「CR灯光」本来就没有 visibleRefract，它改为可选；这里用必需的 intensity 测缺属性
            del key.intensity
            return key

        self.runtime.light.classes[0].factory = create
        with self.runner._item_lights(self.item, self.bounds):
            self.assertFalse(self.created[0].valid or self.created[0].target.valid)
            self.assertEqual(self.lamps[0].intensity, 10)
        self.assertTrue(any("intensity" in text for text in self.runner._calibration_unsupported))
        self.assertFalse(any(call.args[1] == "intensity" and call.args[0] is self.created[0]
                             for call in self.runtime.setProperty.call_args_list))

    def test_mid_setup_failure_restores_already_scaled_fill(self):
        self.prepare()

        def set_prop(obj, prop, value):
            if obj is self.lamps[1] and prop == "multiplier" and value != 4:
                raise RuntimeError("模拟补光设置失败")
            setattr(obj, prop, value)

        self.runtime.setProperty.side_effect = set_prop
        with self.runner._item_lights(self.item, self.bounds):
            self.assertEqual(self.lamps[0].intensity, 10)
            self.assertFalse(self.created[0].valid)
        self.assertTrue(any("模拟补光设置失败" in text for text in self.runner._calibration_unsupported))

    def test_missing_fill_property_and_color_logged(self):
        self.prepare()
        del self.lamps[1].multiplier
        del self.lamps[0].blackbodyTemp
        with self.runner._item_lights(self.item, self.bounds):
            self.assertEqual(self.created[0].intensity, 20)
        self.assertTrue(any("blackbodyTemp" in text for text in self.runner._calibration_unsupported))
        self.assertTrue(any("没有可调的灯光强度" in text for text in self.runner._calibration_unsupported))

    def test_degenerate_bbox_and_invalid_config_skip_without_creation(self):
        self.prepare()
        with self.runner._item_lights(self.item, ((0, 0, 0), (0, 0, 0))):
            pass
        for value in (0, -1, True, math.nan, math.inf, "20"):
            self.runner.config["key_light_intensity"] = value
            with self.runner._item_lights(self.item, self.bounds):
                pass
        self.assertFalse(self.created)
        self.assertEqual(self.lamps[0].intensity, 10)

    def test_restore_failure_stops_task(self):
        self.prepare()

        def set_prop(obj, prop, value):
            if obj is self.lamps[0] and prop == "intensity" and value == 10:
                raise RuntimeError("模拟强度恢复失败")
            setattr(obj, prop, value)

        self.runtime.setProperty.side_effect = set_prop
        with self.assertRaises(self.module.SceneRestoreError):
            with self.runner._item_lights(self.item, self.bounds):
                pass
        self.assertFalse(self.created[0].valid or self.created[0].target.valid)

    def test_delete_failure_stops_task(self):
        self.prepare()
        self.runtime.delete.side_effect = RuntimeError("模拟删除失败")
        with self.assertRaises(self.module.SceneRestoreError):
            with self.runner._item_lights(self.item, self.bounds):
                pass
        self.assertEqual(self.lamps[0].intensity, 10)

    def test_manifest_records_each_item_actual_mm_and_skips(self):
        self.prepare()
        self.runner._scene_manifest = {"materials": [], "unsupported": self.runner._calibration_unsupported}
        self.runner._calibration_dir = mock.Mock(return_value=Path("模拟目录"))
        with mock.patch.object(core, "atomic_write_json") as write:
            with self.runner._item_lights(self.item, self.bounds, 10):
                applied = self.runner._scene_manifest["lighting_applied"]["1"]
                self.assertEqual(applied["pos_mm"], [v * 10 for v in self.created[0].pos])
                self.assertEqual((applied["intensity"], applied["fill_ratio"]), (20, 0.625))
                self.assertIn("右前方", applied["description"])
            with self.runner._item_lights({"no": 2, "lighting": None}, self.bounds):
                self.assertIsNone(self.runner._scene_manifest["lighting_applied"]["2"])
            self.runtime.light.classes = []
            with self.runner._item_lights({"no": 3, "lighting": self.item["lighting"]}, self.bounds):
                self.assertFalse(self.runner._scene_manifest["lighting_applied"]["3"]["applied"])
            self.assertEqual(set(self.runner._scene_manifest["lighting_applied"]), {"1", "2", "3"})
            self.assertEqual(write.call_count, 3)

    def test_no_scene_save_calls_or_english_light_global(self):
        tree = ast.parse((existing.MAX_DIR / "wb_runner.py").read_text(encoding="utf-8"))
        forbidden = {"savemaxfile", "savenodes", "saveplus", "holdmaxfile", "savescene", "saveas"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
                self.assertNotIn(name.casefold(), forbidden)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "rt":
                self.assertNotEqual(node.attr, "CoronaLight")

    def test_mixed_lighting_and_ai_only_items_restore_original_each_time(self):
        self.prepare()
        self.runner._overrides = core.clamp_overrides({"lights": {"intensity_mul": 1.6,
                            "per_light": {"模板主灯": {"intensity_mul": 2}}}})
        with self.runner._item_lights(self.item, self.bounds):
            self.assertAlmostEqual(self.lamps[0].intensity, 10 * 0.625 * 1.15)
        with self.runner._item_lights({"no": 2, "lighting": None}, self.bounds):
            self.assertEqual(self.lamps[0].intensity, 32)
            self.assertFalse(self.created[0].valid)
        self.assertEqual(self.lamps[0].intensity, 10)

    def test_render_job_without_ai_collects_only_template_lights_before_merge(self):
        self.prepare()
        self.runner._probe_lighting = mock.Mock()
        self.runner.job.update(mode="final", template_max="模板.max", max_file="产品.max",
            template_anchor={"x_mm": 11.5, "back_y_mm": -16, "floor_z_mm": 30},
            items=[dict(self.item), {"no": 2, "lighting": {"azimuth_deg": -45, "contrast": 1}},
                   {"no": 3, "lighting": None}])
        for item in self.runner.job["items"]:
            item["camera"] = {"frame": "PF1", "position_mm": [0, 0, 10], "target_mm": [0, 0, 0]}
        self.runner.status = {"items": [{"state": "pending"} for _ in range(3)]}
        self.runtime.objects = list(self.lamps)
        self.runtime.superClassOf.side_effect = lambda node: self.runtime.Light
        product_lamp = Node(name="产品自带灯", intensity=99)

        def merge(path):
            self.runtime.objects.append(product_lamp)
            return ["产品网格", product_lamp]

        self.runner._merge = mock.Mock(side_effect=merge)
        self.runner._load = self.runner._remove_template_cameras = mock.Mock()
        self.runner._mm_per_unit = mock.Mock(return_value=1)
        self.runner._geometry = mock.Mock(side_effect=[[], ["产品网格"]])
        self.runner._bbox = mock.Mock(return_value=self.bounds)
        self.runner._move_roots = self.runner._material_log = self.runner._touch = mock.Mock()
        self.runner._smallshot_time = lambda: nullcontext()
        observed = []

        def render(item, origin, scale):
            observed.append((item["no"], self.lamps[0].intensity, origin, scale))
            self.assertEqual(product_lamp.intensity, 99)
            if item["no"] == 1:
                raise RuntimeError("模拟首项失败，检查下一项恢复")
            return ""

        self.runner._render_item = mock.Mock(side_effect=render)
        self.runner.render_job()
        self.runner._probe_lighting.assert_not_called()
        self.assertEqual(observed, [(1, 6.25, (11.5, -18, 36), 1),
                                    (2, 2.5, (11.5, -18, 36), 1),
                                    (3, 10, (11.5, -18, 36), 1)])
        self.assertEqual([item["state"] for item in self.runner.status["items"]], ["failed", "done", "done"])
        self.assertEqual(len(self.created), 2)
        self.assertTrue(all(not key.valid and not key.target.valid for key in self.created))


if __name__ == "__main__":
    unittest.main()
