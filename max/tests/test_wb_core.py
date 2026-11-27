"""使用普通 Python 验证文件协议、坐标和单位。"""

import importlib.util
import json
import math
import os
from contextlib import nullcontext
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest import mock
from uuid import uuid4


MAX_DIR = Path(__file__).resolve().parents[1]
TMP_DIR = MAX_DIR.parent / "tests" / "tmp" / "max-tests"
sys.path.insert(0, str(MAX_DIR))
import wb_core as core


class VariantNodeSelectionTests(unittest.TestCase):
    def test_filters_exact_mesh_names_and_only_prunes_groups_without_kept_descendants(self):
        names = {3:"柜门", 4:"另一柜体", 7:"柜门", 8:"Door", 9:"door"}
        parents = {1:None, 2:1, 3:2, 4:5, 5:6, 6:1, 7:None, 8:None, 9:None, 10:None}
        keep, removed = core.variant_node_selection(names, parents, {1, 2, 5, 6, 10}, ["柜门", "Door"])
        self.assertEqual(keep, {3, 7, 8})
        self.assertEqual(removed, {4, 5, 6, 9})

    def test_no_match_raises_actionable_error(self):
        with self.assertRaisesRegex(core.VariantNodesNotFound,
                                    "场景里找不到这个柜子的物体，模型可能改过，请在网页里重新识别柜子"):
            core.variant_node_selection({1:"旧节点"}, {1:None}, set(), ["已改名的节点"])


class CoordinatesTests(unittest.TestCase):
    def test_pf1_axes(self):
        self.assertEqual(core.pf1_to_max((1, 2, 3)), (1, -3, 2))

    def test_front_faces_negative_max_y(self):
        self.assertEqual(core.pf1_to_max((0, 0, 1000)), (0, -1000, 0))
        self.assertEqual(core.max_to_pf1((0, -1000, 0)), (0, 0, 1000))

    def test_round_trip_at_shifted_origin_all_units(self):
        for scale in (1, 10, 1000):
            with self.subTest(mm_per_unit=scale):
                point, origin = (1520.3, 640.0, 2170.9), (12, -35, 8)
                actual = core.max_to_pf1(core.pf1_to_max(point, origin, scale), origin, scale)
                for a, b in zip(actual, point):
                    self.assertAlmostEqual(a, b)

    def test_millimeter_system(self):
        self.assertEqual(core.mm_to_units(1250, 1), 1250)
        self.assertEqual(core.units_to_mm(1250, 1), 1250)

    def test_centimeter_system(self):
        self.assertEqual(core.mm_to_units(1250, 10), 125)
        self.assertEqual(core.units_to_mm(125, 10), 1250)

    def test_meter_system(self):
        self.assertEqual(core.mm_to_units(1250, 1000), 1.25)
        self.assertEqual(core.units_to_mm(1.25, 1000), 1250)

    def test_invalid_scale(self):
        for scale in (0, -1, math.nan, math.inf):
            with self.subTest(scale=scale), self.assertRaises(ValueError):
                core.pf1_to_max((1, 2, 3), mm_per_unit=scale)

    def test_invalid_vector(self):
        for value in ((1, 2), (1, 2, math.nan)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.max_to_pf1(value)


class FovTests(unittest.TestCase):
    def test_portrait_three_by_four(self):
        # vfov=90 时 tan(vfov/2)=1，可直接核验横向半角的正切。
        hfov = core.horizontal_fov_deg(90, 3000, 4000)
        self.assertAlmostEqual(hfov, 73.73979529168804)
        self.assertAlmostEqual(math.tan(math.radians(hfov) / 2), 0.75)

    def test_landscape_four_by_three(self):
        self.assertAlmostEqual(core.horizontal_fov_deg(90, 4000, 3000), 106.26020470831196)

    def test_fov_is_independent_of_pixel_count(self):
        self.assertAlmostEqual(core.horizontal_fov_deg(27, 600, 800),
                               core.horizontal_fov_deg(27, 3000, 4000))

    def test_invalid_fov_or_size(self):
        for args in ((0, 3, 4), (180, 3, 4), (27, 0, 4), (27, 3, -4), (math.nan, 3, 4)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                core.horizontal_fov_deg(*args)


class VerticalCorrectionTests(unittest.TestCase):
    def test_horizontal_target_and_shift_use_actual_aspect_and_coordinates(self):
        camera = {"frame": "PF1", "position_mm": [420, 520, 830], "target_mm": [20, 80, 30],
                  "fov_y_deg": 27, "vertical_correction": True, "elevation_deg": 78}
        for width, height in [(800, 600), (600, 800)]:
            with self.subTest(width=width):
                result = core.camera_vertical_correction(camera, width, height)
                self.assertEqual(result["target_mm"], (20, 520, 30))
                self.assertEqual(result["horizontal_fov_deg"], core.horizontal_fov_deg(27, width, height))
                ty = math.tan(math.radians(27) / 2)
                image_ndc_shift = 2 * width / height * result["image_shift_film_width"]
                depth = math.hypot(400, 800)
                self.assertAlmostEqual(-440 / (depth * ty) + image_ndc_shift, 0)
                self.assertGreater(image_ndc_shift, 0)

    def test_inclusive_positive_and_negative_45_and_disabled_defaults(self):
        camera = {"frame": "PF1", "position_mm": [600, 1000, 800], "target_mm": [0, 0, 0],
                  "fov_y_deg": 27, "elevation_deg": 0}
        self.assertIsNone(core.camera_vertical_correction(camera, 800, 600))
        self.assertIsNone(core.camera_vertical_correction(dict(camera, vertical_correction=False), 800, 600))
        camera["vertical_correction"] = True
        for sign in (-1, 1):
            camera["position_mm"][1] = sign * 1000
            result = core.camera_vertical_correction(camera, 800, 600)
            self.assertEqual(math.copysign(1, result["image_shift_film_width"]), sign)
            camera["position_mm"][1] = sign * 1000.0001
            self.assertIsNone(core.camera_vertical_correction(camera, 800, 600))


class BoundingBoxTests(unittest.TestCase):
    def test_center_and_bottom(self):
        self.assertEqual(core.bbox_center((10, -40, 30), (110, 20, 130)), (60, -10, 80))
        self.assertEqual(core.bottom_center((10, -40, 30), (110, 20, 130)), (60, -10, 30))

    def test_anchor_placement_centimeters(self):
        anchor = {"x_mm": 1000, "back_y_mm": 2000, "floor_z_mm": 0}
        result = core.placement_from_bbox((10, -40, 30), (110, 20, 130), anchor, 10)
        self.assertEqual(result["delta_max"], (40, 180, -30))
        self.assertEqual(result["placed_origin_max"], (100, 170, 50))
        shifted = tuple(a + b for a, b in zip(result["back_bottom_center_max"], result["delta_max"]))
        self.assertEqual(shifted, (100, 200, 0))

    def test_wall_anchor_all_system_units(self):
        anchor = {"x_mm": 444, "back_y_mm": 461, "floor_z_mm": 0}
        for scale in (1, 10, 1000):
            with self.subTest(scale=scale):
                low = tuple(v / scale for v in (-600, -300, 30))
                high = tuple(v / scale for v in (600, 100, 880))
                result = core.placement_from_bbox(low, high, anchor, scale)
                delta = result["delta_max"]
                self.assertAlmostEqual(((low[0] + high[0]) / 2 + delta[0]) * scale, 444)
                self.assertAlmostEqual((high[1] + delta[1]) * scale, 461)
                self.assertAlmostEqual((low[2] + delta[2]) * scale, 0)
                # y 中心应为 261 mm，而不是墙后平面的 461 mm。
                self.assertAlmostEqual(result["placed_origin_max"][1] * scale, 261)

    def test_wall_placement_rejects_legacy_triplet(self):
        with self.assertRaises(ValueError):
            core.placement_from_bbox((0, 0, 0), (1, 1, 1), [444, 461, 0], 1)

    def test_union_bbox(self):
        self.assertEqual(core.union_bbox([((1, 2, 3), (4, 5, 6)), ((-1, 4, 1), (2, 9, 5))]),
                         ((-1, 2, 1), (4, 9, 6)))

    def test_empty_and_inverted_bbox_rejected(self):
        with self.assertRaises(ValueError):
            core.union_bbox([])
        with self.assertRaises(ValueError):
            core.bbox_center((2, 0, 0), (1, 2, 3))

    def test_bbox_document_exact_spec(self):
        self.assertEqual(core.bbox_document((-60, -20, 0), (60, 20, 85), 37, 10), {
            "units": "mm", "max_world_bbox_mm": {"min": [-600, -200, 0], "max": [600, 200, 850]},
            "size_mm": {"x": 1200, "y_up": 850, "z_depth": 400}, "mesh_count": 37})

    def test_bbox_document_meter_system(self):
        self.assertEqual(core.bbox_document((0, 0, 0), (1.2, 0.4, 0.85), 1, 1000)["size_mm"],
                         {"x": 1200, "y_up": 850, "z_depth": 400})

    def test_mesh_count_validation(self):
        for count in (-1, 1.5, True):
            with self.subTest(count=count), self.assertRaises(ValueError):
                core.bbox_document((0, 0, 0), (1, 1, 1), count)


class FilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 不删除任何测试产物；每次运行保留在独立目录，可供核查。
        cls.root = TMP_DIR / ("max-" + uuid4().hex[:8])
        cls.root.mkdir(parents=True)

    def setUp(self):
        self.directory = self.root / "{:02d}".format(len(list(self.root.iterdir())))
        self.directory.mkdir()

    def write(self, name, data):
        path = self.directory / name
        core.atomic_write_json(path, data)
        return path

    def job(self, job_id, date):
        return self.write(job_id + ".json", {"job_id": job_id, "created_at": date, "type": "export_preview"})

    def test_config_relative_paths(self):
        path = self.write("config.json", {"exchange_dir": "exchange", "template_max": "模板.max",
                                          "render_preset_rps": "预设.rps", "template_anchor":
                                          {"x_mm": 444, "back_y_mm": 461, "floor_z_mm": 0}})
        config = core.load_config(path)
        self.assertEqual(config["exchange_dir"], str((self.directory / "exchange").resolve()))
        self.assertEqual(config["template_max"], str((self.directory / "模板.max").resolve()))
        self.assertEqual(config["render_preset_rps"], str((self.directory / "预设.rps").resolve()))
        self.assertEqual(config["template_anchor"], {"x_mm": 444, "back_y_mm": 461, "floor_z_mm": 0})

    def test_config_null_preset_preserved(self):
        config = core.load_config(self.write("config.json", {"render_preset_rps": None}))
        self.assertIsNone(config["render_preset_rps"])
        self.assertNotIn("template_anchor", config)

    def test_legacy_config_anchor_is_rejected(self):
        path = self.write("config.json", {"template_anchor_mm": [1, 2, 3]})
        with self.assertRaises(ValueError):
            core.load_config(path)

    def test_legacy_job_anchor_is_rejected(self):
        self.write("old.json", {"job_id": "old", "created_at": "2026-11-19T10:00:00Z",
                                "template_anchor_mm": [1, 2, 3]})
        errors = []
        self.assertIsNone(core.next_job(self.directory, lambda path, exc: errors.append(exc)))
        self.assertEqual(len(errors), 1)

    def test_jobs_sort_and_skip_terminal(self):
        self.job("late", "2026-11-19T11:00:00+00:00")
        self.job("first", "2026-11-19T09:00:00+00:00")
        self.job("done", "2026-11-19T08:00:00+00:00")
        self.job("failed", "2026-11-19T07:00:00+00:00")
        self.write("done.status.json", {"state": "done"})
        self.write("failed.status.json", {"state": "failed"})
        self.assertEqual([j.data["job_id"] for j in core.pending_jobs(self.directory)], ["first", "late"])

    def test_job_sort_uses_actual_timezone_instant(self):
        self.job("earlier", "2026-11-19T18:00:00+09:00")
        self.job("later", "2026-11-19T10:00:00+00:00")
        self.assertEqual(core.next_job(self.directory).data["job_id"], "earlier")

    def test_job_tie_uses_filename(self):
        for name in ("b", "a"):
            self.job(name, "2026-11-19T10:00:00Z")
        self.assertEqual(core.next_job(self.directory).data["job_id"], "a")

    def test_waiting_and_interrupted_jobs_retry(self):
        for name, state in (("a", "waiting_lock"), ("b", "running")):
            self.job(name, "2026-11-19T10:00:00Z")
            self.write(name + ".status.json", {"state": state})
        self.assertEqual(len(core.pending_jobs(self.directory)), 2)

    def test_temporary_and_backup_files_not_scanned(self):
        self.write("partial.json.tmp", {"job_id": "partial"})
        self.write("old.json.prev", {"job_id": "old"})
        self.assertIsNone(core.next_job(self.directory))

    def test_corrupt_status_never_silently_reexecuted(self):
        self.job("bad", "2026-11-19T10:00:00Z")
        (self.directory / "bad.status.json").write_text("{", encoding="utf-8")
        errors = []
        self.assertIsNone(core.next_job(self.directory, lambda p, e: errors.append((p, e))))
        self.assertEqual(len(errors), 1)

    def test_bad_job_does_not_block_valid_queue(self):
        self.write("bad.json", {"job_id": "different", "created_at": "无效"})
        self.job("good", "2026-11-19T10:00:00Z")
        errors = []
        self.assertEqual(core.next_job(self.directory, lambda p, e: errors.append(p)).data["job_id"], "good")
        self.assertEqual(len(errors), 1)

    def test_atomic_replace_uses_same_directory_tmp(self):
        path = self.directory / "job.status.json"
        actual_replace = os.replace
        calls = []

        def inspect_replace(source, target):
            self.assertEqual(Path(source).parent, path.parent)
            self.assertTrue(Path(source).name.endswith(".tmp"))
            self.assertEqual(Path(target), path)
            self.assertEqual(core.read_json(source)["state"], "done")
            calls.append((source, target))
            actual_replace(source, target)

        with mock.patch.object(core.os, "replace", side_effect=inspect_replace):
            core.write_status(path, core.new_status({"job_id": "job"}) | {"state": "done"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(core.read_status(path)["state"], "done")

    def test_failed_replace_retains_old_status_and_tmp(self):
        path = self.write("job.status.json", {"state": "running"})
        with mock.patch.object(core.os, "replace", side_effect=PermissionError("文件被占用")):
            with self.assertRaises(PermissionError):
                core.atomic_write_json(path, {"state": "done"})
        self.assertEqual(core.read_status(path)["state"], "running")
        self.assertEqual(core.read_json(next(self.directory.glob("job.status.json.*.tmp")))["state"], "done")
        self.assertEqual(list(self.directory.glob("*.bak")), [])

    def test_atomic_overwrites_do_not_create_backups(self):
        path = self.write("data.json", {"version": 1})
        core.atomic_write_json(path, {"version": 2})
        core.atomic_write_json(path, {"version": 3})
        self.assertEqual(list(self.directory.glob("data.json.*")), [])
        self.assertEqual(core.read_json(path)["version"], 3)

    def test_archive_removes_stale_output_from_target_only_by_move(self):
        path = self.write("mask.json", {"version": 1})
        backup = core.backup_existing(path, move=True)
        self.assertFalse(path.exists())
        self.assertEqual(core.read_json(backup)["version"], 1)

    def test_status_schema_and_log_limit(self):
        status = core.new_status({"job_id": "job", "items": [
            {"no": 1, "out_image": "成图.jpg", "out_mask": "遮罩.jpg"}]})
        for index in range(220):
            core.add_status_log(status, "日志 {}\n第二行".format(index))
        path = self.directory / "job.status.json"
        core.write_status(path, status)
        loaded = core.read_status(path)
        self.assertEqual(len(loaded["log"]), 200)
        self.assertEqual(loaded["items"][0]["state"], "pending")
        self.assertTrue(loaded["log"][-1].endswith("第二行"))
        self.assertTrue(loaded["updated_at"])

    def test_runner_log_appends_and_rotates_three_files(self):
        core.append_runner_log(self.directory, "\n".join("日志 {}".format(i) for i in range(250)))
        lines = (self.directory / "runner.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 250)
        self.assertTrue(lines[0].endswith("日志 0"))
        self.assertTrue(lines[-1].endswith("日志 249"))
        path = self.directory / "runner.log"
        for index in range(5):
            with path.open("ab") as stream:
                stream.write(bytes(core.RUNNER_LOG_BYTES))
            core.append_runner_log(self.directory, f"轮换 {index}")
        self.assertEqual(sorted(p.name for p in self.directory.glob("runner.log*")),
                         ["runner.log", "runner.log.1", "runner.log.2", "runner.log.3"])
        self.assertIn("轮换 4", path.read_text(encoding="utf-8"))
        self.assertIn("轮换 1", (self.directory / "runner.log.3").read_text(encoding="utf-8"))
        self.assertEqual(list(self.directory.glob("*.bak")), [])

    def test_runner_heartbeat_refreshes_while_busy_and_stops(self):
        errors = []
        heartbeat = core.RunnerHeartbeat(self.directory, "fake", errors.append, interval=.03)
        self.addCleanup(heartbeat.stop)
        path = self.directory / "runner.heartbeat.json"
        renewed = threading.Event()
        writer = core.atomic_write_json

        def observe(target, document):
            writer(target, document)
            if threading.current_thread() is heartbeat.thread and document["busy"]:
                renewed.set()

        with mock.patch.object(core, "atomic_write_json", side_effect=observe):
            heartbeat.start()
            self.assertEqual(core.read_json(path)["job_id"], None)
            heartbeat.set_job("job")
            self.assertTrue(renewed.wait(2))
            self.assertTrue(core.read_json(path)["busy"])
            heartbeat.stop()
            heartbeat.thread.join(timeout=2)
        self.assertTrue(core.read_json(path)["stopped"])
        self.assertEqual(core.read_json(path)["runner_version"], "fake")
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), [path.name])
        self.assertEqual(errors, [])

    def test_bbox_json_roundtrip(self):
        document = core.bbox_document((0, -400, 20), (1200, 0, 870), 37)
        self.assertEqual(core.read_json(self.write("bbox.json", document)), document)

    def test_nonfinite_json_rejected_before_write(self):
        path = self.directory / "invalid.json"
        with self.assertRaises(ValueError):
            core.atomic_write_json(path, {"value": math.nan})
        self.assertFalse(path.exists())


class UpdatedPolicyTests(unittest.TestCase):
    def test_anchor_task_overrides_config(self):
        config = {"template_anchor": {"x_mm": 444, "back_y_mm": 461, "floor_z_mm": 0}}
        task = {"template_anchor": {"x_mm": 10, "back_y_mm": 20, "floor_z_mm": 30}}
        self.assertEqual(core.template_anchor_from(task, config), task["template_anchor"])
        self.assertEqual(core.template_anchor_from({}, config), config["template_anchor"])

    def test_anchor_requires_explicit_valid_object(self):
        for anchor in (None, [1, 2, 3], {}, {"x_mm": 1, "back_y_mm": 2},
                       {"x_mm": math.nan, "back_y_mm": 2, "floor_z_mm": 3}):
            with self.subTest(anchor=anchor), self.assertRaises(ValueError):
                core.validate_template_anchor(anchor)
        with self.assertRaises(ValueError):
            core.template_anchor_from({})
        with self.assertRaises(ValueError):
            core.template_anchor_from({"template_anchor_mm": [1, 2, 3]})

    def test_camera_azimuth_uses_authoritative_positions(self):
        for x, z, expected in ((0, 10, 0), (10, 0, 90), (0, -10, 180), (-10, 0, -90)):
            camera = {"frame": "PF1", "position_mm": [x + 3, 40, z + 5],
                      "target_mm": [3, 10, 5], "azimuth_deg": 17}
            with self.subTest(expected=expected):
                self.assertAlmostEqual(core.camera_azimuth_deg(camera), expected)

    def test_camera_without_horizontal_direction_is_rejected(self):
        with self.assertRaises(ValueError):
            core.camera_azimuth_deg({"frame": "PF1", "position_mm": [0, 1, 0], "target_mm": [0, 0, 0]})

    def test_back_view_strict_angle_boundary(self):
        for angle, expected in ((0, False), (100, False), (-100, False),
                                (100.001, True), (-100.001, True), (180, True)):
            with self.subTest(angle=angle):
                self.assertEqual(core.should_hide_template_geometry((0, 461, 0), (1, 500, 1),
                                                                    461, angle), expected)

    def test_back_view_wall_plane_is_inclusive(self):
        self.assertTrue(core.should_hide_template_geometry((0, 461, 0), (1, 500, 1), 461, 150))
        self.assertTrue(core.should_hide_template_geometry((0, 471, 0), (1, 500, 1), 461, -150))
        # 跨越背面平面的几何体不能隐藏，判断必须用最小 y。
        self.assertFalse(core.should_hide_template_geometry((0, 460.999, 0), (1, 500, 1), 461, 150))

    def test_back_view_wall_all_system_units(self):
        for scale in (1, 10, 1000):
            with self.subTest(scale=scale):
                self.assertTrue(core.should_hide_template_geometry((0, 471 / scale, 0),
                                                                   (1, 500 / scale, 1), 461, 180, scale))
                self.assertFalse(core.should_hide_template_geometry((0, 450 / scale, 0),
                                                                    (1, 500 / scale, 1), 461, 180, scale))

    def test_back_view_nonfinite_inputs_rejected(self):
        for plane, angle in ((math.nan, 150), (461, math.inf)):
            with self.subTest(plane=plane, angle=angle), self.assertRaises(ValueError):
                core.should_hide_template_geometry((0, 471, 0), (1, 500, 1), plane, angle)

    def test_smallshot_seconds_to_milliseconds(self):
        self.assertEqual(core.smallshot_time_limit_ms(25), 25000)
        self.assertEqual(core.smallshot_time_limit_ms(0.001), 1)
        self.assertEqual(core.smallshot_time_limit_ms(0.0015), 2)

    def test_smallshot_invalid_limit(self):
        for value in (0, -1, math.nan, math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.smallshot_time_limit_ms(value)

    def test_mask_exact_names_override_fuzzy_and_order(self):
        elements = [{"index": 0, "names": ["遮罩_线框颜色"], "enabled": True},
                    {"index": 1, "names": ["GenericMask"], "enabled": True},
                    {"index": 2, "names": ["遮罩_ID"], "enabled": False}]
        self.assertEqual(core.select_mask_elements(elements),
                         {"id_index": 2, "wire_index": 0, "id_exact": True})

    def test_mask_fuzzy_never_uses_known_wire_channel(self):
        elements = [{"index": 0, "names": ["遮罩_线框颜色"], "enabled": True},
                    {"index": 1, "names": ["CMasking_ID"], "enabled": True}]
        selected = core.select_mask_elements(elements)
        self.assertEqual(selected, {"id_index": 1, "wire_index": 0, "id_exact": False})
        self.assertIsNone(core.select_mask_elements(elements[:1])["id_index"])

    def test_mask_exact_match_may_come_from_class_name(self):
        selected = core.select_mask_elements([
            {"index": 1, "names": ["遮罩_ID", "重命名的元素"], "enabled": True}])
        self.assertTrue(selected["id_exact"])
        self.assertEqual(selected["id_index"], 1)


class RunnerContractTests(unittest.TestCase):
    """仅用标准库模拟调用契约和异常恢复，不代表 Max 或 Corona 实机测试。"""

    def setUp(self):
        fake_pymxs = types.ModuleType("pymxs")
        self.runtime = mock.Mock()
        fake_pymxs.runtime = self.runtime
        fake_pymxs.byref = lambda value: value
        fake_qt = types.ModuleType("PySide6")
        fake_qt.QtCore, fake_qt.QtWidgets = mock.Mock(), mock.Mock()
        spec = importlib.util.spec_from_file_location("_whitebg_runner_contract_test", MAX_DIR / "wb_runner.py")
        self.module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"pymxs": fake_pymxs, "PySide6": fake_qt}):
            spec.loader.exec_module(self.module)
        self.runner = object.__new__(self.module.Runner)
        self.runner.log = mock.Mock()
        self.runner.flush = mock.Mock()
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        self.runner.exchange = TMP_DIR / ("runner-" + uuid4().hex[:8])
        self.runner.config = {"smallshot_time_limit_sec": 25}
        self.runner.job = {"mode": "smallshot", "time_limit_sec": 25, "task_id": "任务1"}
        self.runtime.hasProperty.side_effect = lambda obj, name: hasattr(obj, name)
        self.runtime.getProperty.side_effect = lambda obj, name: getattr(obj, name)
        self.runtime.setProperty.side_effect = lambda obj, name, value: setattr(obj, name, value)
        self.runtime.isValidNode.return_value = True

    def test_uncorrected_uses_original_target_camera_parameters(self):
        self.runtime.Point3.side_effect = lambda *values: values
        spec = {"frame": "PF1", "position_mm": [420, 520, 830], "target_mm": [20, 80, 30], "fov_y_deg": 27}
        for flag in ({}, {"vertical_correction": False},
                     {"vertical_correction": True, "position_mm": [420, 2000, 830]}):
            self.runtime.reset_mock()
            camera, target = self.runner._camera(
                {"camera": {**spec, **flag}, "width": 800, "height": 600, "no": 2}, (10, 20, 30), 10)
            self.runtime.TargetObject.assert_called_once_with(name="WhiteBG_目标_2", pos=(12, 17, 38))
            self.runtime.TargetCamera.assert_called_once_with(name="WhiteBG_相机_2", target=target,
                                                              pos=(52, -63, 230 if flag.get("vertical_correction") else 82))
            self.runtime.Physical.assert_not_called()
            self.assertEqual(camera.fovType, 1)
            self.assertEqual(camera.rollAngle, 0)
            self.assertAlmostEqual(camera.fov, 35.50025864761136)

    def test_corrected_physical_camera_uses_horizontal_target_and_percent_shift(self):
        self.runtime.Point3.side_effect = lambda *values: values
        spec = {"frame": "PF1", "position_mm": [420, 520, 830], "target_mm": [20, 80, 30],
                "fov_y_deg": 27, "vertical_correction": True}
        camera, target = self.runner._camera({"camera": spec, "width": 800, "height": 600, "no": 2},
                                             (10, 20, 30), 10)
        # Physical 的 targeted=True 不会自动生成目标点（Max 实测），先建目标点再传入。
        self.runtime.TargetObject.assert_called_once_with(name="WhiteBG_目标_2", pos=(12, 17, 82))
        self.runtime.Physical.assert_called_once_with(name="WhiteBG_相机_2", pos=(52, -63, 82),
                                                      targeted=True, target=target)
        self.runtime.TargetCamera.assert_not_called()
        self.assertTrue(camera.specify_fov)
        self.assertFalse(camera.auto_vertical_tilt_correction)
        expected = core.camera_vertical_correction(spec, 800, 600)
        self.assertEqual(camera.fov, expected["horizontal_fov_deg"])
        self.assertEqual(camera.vertical_shift, 100 * expected["image_shift_film_width"])

    def test_smallshot_changes_only_exact_time_property_and_restores_on_error(self):
        renderer = types.SimpleNamespace(progressive_timelimit=0, progressive_passlimit=7,
                                         adaptivity_targeterror=2, bg_overrideReflect=True)
        self.runtime.renderers.current = renderer
        self.runtime.classOf.return_value = "CoronaRenderer"
        with self.assertRaises(RuntimeError):
            with self.runner._smallshot_time():
                self.assertEqual(renderer.progressive_timelimit, 25000)
                raise RuntimeError("模拟渲染异常")
        self.assertEqual(vars(renderer), {"progressive_timelimit": 0, "progressive_passlimit": 7,
                                          "adaptivity_targeterror": 2, "bg_overrideReflect": True})
        self.assertEqual([call.args[1:] for call in self.runtime.setProperty.call_args_list],
                         [("progressive_timelimit", 25000), ("progressive_timelimit", 0)])

    def test_final_does_not_write_renderer_properties(self):
        self.runner.job["mode"] = "final"
        self.runtime.classOf.return_value = "CoronaRenderer"
        self.runtime.renderers.current = types.SimpleNamespace(progressive_timelimit=0)
        with self.runner._smallshot_time():
            pass
        self.runtime.setProperty.assert_not_called()

    def test_missing_time_property_does_not_try_aliases(self):
        self.runtime.classOf.return_value = "CoronaRenderer"
        self.runtime.renderers.current = types.SimpleNamespace(main_timeLimit=0)
        with self.runner._smallshot_time():
            pass
        self.runtime.setProperty.assert_not_called()

    def test_back_view_restores_original_node_flags_on_render_error(self):
        nodes = [types.SimpleNamespace(isNodeHidden=False), types.SimpleNamespace(isNodeHidden=True),
                 types.SimpleNamespace(isNodeHidden=False)]
        self.runner._bbox = mock.Mock(side_effect=[((0, 471, 0), (1, 500, 1)),
                                                   ((0, 471, 0), (1, 500, 1)),
                                                   ((0, 450, 0), (1, 500, 1))])
        item = {"camera": {"frame": "PF1", "position_mm": [0, 0, -10], "target_mm": [0, 0, 0]}}
        with self.assertRaises(RuntimeError):
            with self.runner._back_view_visibility(item, nodes, {"back_y_mm": 461}, 1):
                self.assertEqual([node.isNodeHidden for node in nodes], [True, True, False])
                raise RuntimeError("模拟渲染异常")
        self.assertEqual([node.isNodeHidden for node in nodes], [False, True, False])

    def test_front_view_does_not_change_template_visibility(self):
        self.runner._bbox = mock.Mock()
        item = {"camera": {"frame": "PF1", "position_mm": [0, 0, 10], "target_mm": [0, 0, 0]}}
        with self.runner._back_view_visibility(item, [types.SimpleNamespace(isNodeHidden=False)],
                                                {"back_y_mm": 461}, 1):
            pass
        self.runner._bbox.assert_not_called()

    def prepare_masks(self):
        nodes = [types.SimpleNamespace(class_name="遮罩_线框颜色", elementName="遮罩_线框颜色", enabled=True),
                 types.SimpleNamespace(class_name="遮罩_ID", elementName="遮罩_ID", enabled=True)]
        manager = self.runtime.maxOps.GetCurRenderElementMgr.return_value
        manager.NumRenderElements.return_value = 2
        manager.GetRenderElement.side_effect = lambda index: nodes[index]
        manager.GetElementsActive.return_value = True
        manager.GetRenderElementFilename.side_effect = lambda index: "原路径{}.jpg".format(index)
        self.runtime.classOf.side_effect = lambda obj: obj.class_name
        self.runner._output = mock.Mock(side_effect=lambda path, suffix: str(path))
        self.runner.job["mode"] = "final"
        item = {"no": 3, "out_image": str(MAX_DIR / "3.jpg"), "out_mask": str(MAX_DIR / "3_遮罩_ID.jpg")}
        return manager, item

    def test_mask_id_and_wire_are_routed_separately(self):
        manager, item = self.prepare_masks()
        restore_paths = []
        plan = self.runner._mask_setup(item, restore_paths)
        expected_extra = self.runner.exchange / "tasks" / "任务1" / "final" / "_extra" / "3_遮罩_线框颜色.jpg"
        self.assertEqual([(r["index"], r["path"]) for r in plan["routes"]],
                         [(1, item["out_mask"]), (0, str(expected_extra))])
        self.assertEqual([(index, path) for _, index, path in restore_paths],
                         [(1, "原路径1.jpg"), (0, "原路径0.jpg")])
        manager.SetElementsActive.assert_not_called()

    def test_mask_paths_are_restored_even_when_render_raises(self):
        manager, item = self.prepare_masks()
        item.update(width=600, height=800)
        self.runner._camera = mock.Mock(return_value=(object(), object()))
        self.runner._touch = mock.Mock()
        self.runtime.render.side_effect = RuntimeError("模拟渲染异常")
        with self.assertRaises(RuntimeError):
            self.runner._render_item(item, (0, 0, 0), 1)
        self.assertEqual(manager.SetRenderElementFilename.call_args_list[-2:],
                         [mock.call(0, "原路径0.jpg"), mock.call(1, "原路径1.jpg")])

    def test_render_loop_filters_visible_meshes_and_touches_after_each_item(self):
        anchor = {"x_mm": 0, "back_y_mm": 0, "floor_z_mm": 0}
        camera = {"frame": "PF1", "position_mm": [0, 0, 10], "target_mm": [0, 0, 0]}
        self.runner.job.update(mode="final", template_max="模板.max", max_file="产品.max",
                               template_anchor=anchor, items=[{"no": 1, "camera": camera}, {"no": 2, "camera": camera}])
        self.runner.status = {"items": [{"state": "pending"}, {"state": "pending"}]}
        self.runner._load = mock.Mock()
        self.runner._remove_template_cameras = mock.Mock()
        self.runner._mm_per_unit = mock.Mock(return_value=1)
        self.runtime.objects = []
        self.runner._geometry = mock.Mock(side_effect=[[], ["产品网格"]])
        self.runner._merge = mock.Mock(return_value=["合并节点"])
        self.runner._bbox = mock.Mock(return_value=((-1, -2, 0), (1, 0, 2)))
        self.runner._move_roots = self.runner._material_log = mock.Mock()
        self.runner._smallshot_time = lambda: nullcontext()
        self.runner._render_item = mock.Mock(side_effect=[RuntimeError("第一项失败"), ""])
        self.runner._touch = mock.Mock()
        self.runner.render_job()
        self.assertTrue(self.runner._geometry.call_args_list[1].kwargs["visible_only"])
        self.assertEqual(self.runner._touch.call_count, 2)
        self.assertEqual([item["state"] for item in self.runner.status["items"]], ["failed", "done"])

    def test_render_loop_keeps_only_selected_cabinet_before_bounds_and_placement(self):
        anchor = {"x_mm":0, "back_y_mm":0, "floor_z_mm":0}
        self.runner.job.update(mode="final", template_max="模板.max", max_file="产品.max",
                               template_anchor=anchor, items=[{"no":1}],
                               keep_nodes=["白柜"], variant_name="白色")
        self.runner.status = {"items":[{"state":"pending"}]}
        chosen = types.SimpleNamespace(name="白柜", handle=1, kind=self.runtime.GeometryClass,
                                       group=False, parent=None)
        other_group = types.SimpleNamespace(name="木柜组", handle=3, kind=self.runtime.Helper,
                                            group=True, parent=None)
        other = types.SimpleNamespace(name="木柜", handle=2, kind=self.runtime.GeometryClass,
                                      group=False, parent=other_group)
        self.runtime.objects = []
        self.runtime.superClassOf.side_effect = lambda node: node.kind
        self.runtime.isGroupHead.side_effect = lambda node: node.group
        self.runtime.getHandleByAnim.side_effect = lambda node: node.handle
        self.runner._load = mock.Mock()
        self.runner._remove_template_cameras = mock.Mock()
        self.runner._mm_per_unit = mock.Mock(return_value=1)
        self.runner._merge = mock.Mock(return_value=[chosen, other, other_group])
        self.runner._geometry = mock.Mock(side_effect=lambda nodes, **kwargs: nodes)

        def bounds(nodes):
            self.assertEqual(nodes, [chosen])
            self.assertEqual(self.runtime.delete.call_args_list, [mock.call(other), mock.call(other_group)])
            return ((-1, -2, 0), (1, 0, 2))

        self.runner._bbox = mock.Mock(side_effect=bounds)
        self.runner._move_roots = mock.Mock()
        self.runner._material_log = mock.Mock()
        self.runner._smallshot_time = nullcontext
        self.runner._item_lights = lambda *args: nullcontext()
        self.runner._back_view_visibility = lambda *args: nullcontext()
        self.runner._render_item = mock.Mock(return_value="")
        self.runner._touch = mock.Mock()
        self.runner.render_job()
        self.runner._move_roots.assert_called_once_with([chosen], (0, 0, 0))
        self.runner._render_item.assert_called_once_with({"no":1}, (0, -1, 1), 1)
        self.runner.log.assert_any_call("只保留柜子 白色：保留 1 个、删除 2 个")
        self.assertEqual(self.runner.status["items"][0]["state"], "done")

        self.runner.job["keep_nodes"] = ["过期节点"]
        self.runtime.delete.reset_mock()
        self.runner._bbox.reset_mock()
        with self.assertRaises(core.VariantNodesNotFound):
            self.runner.render_job()
        self.runtime.delete.assert_not_called()
        self.runner._bbox.assert_not_called()


class MaterialsTests(unittest.TestCase):
    def test_name_and_class_case_insensitive(self):
        self.assertEqual(core.material_kinds("门玻璃", "CoronaMtl"), ("玻璃",))
        self.assertEqual(core.material_kinds("", "GLASSMaterial"), ("玻璃",))
        self.assertEqual(core.material_kinds("Steel", "CoronaPhysicalMtl"), ("金属",))
        self.assertEqual(core.material_kinds("玻璃和金属"), ("玻璃", "金属"))

    def test_ordinary_material(self):
        self.assertEqual(core.material_kinds("白色木纹", "CoronaPhysicalMtl"), ())

    def test_core_does_not_load_pymxs(self):
        # 在普通解释器中已经成功导入；同时检查静态依赖，防止以后误加。
        import ast
        tree = ast.parse((MAX_DIR / "wb_core.py").read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        self.assertFalse(any(name == "pymxs" or name.startswith("pymxs.") for name in imports))


class BlankRenderTests(unittest.TestCase):
    """2026-11-20 实测：系统提交内存占满时 Corona 中途放弃，交回全黑成图。"""

    CORONA_LOG = (
        "2026-11-20 23:30:01  Internal error. Please report it to us:\n"
        "Some older error\n"
        "2026-11-20 23:34:13  Internal error. Please report it to us:\n"
        "Bad allocation Exception!\n"
        "Chaos Corona ran out of RAM an was unable to allocate memory it needs for rendering.\n"
        "\n"
        "Debugging details:\n"
        "Wanted to allocate: 183.1 MB\n"
    )

    def write_log(self):
        folder = TMP_DIR
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "corona-{}_errors.txt".format(uuid4().hex)
        path.write_text(self.CORONA_LOG, encoding="utf-8")
        self.addCleanup(path.unlink)
        return path

    def test_sample_rows_inside_image(self):
        rows = core.sample_rows(4000)
        self.assertEqual(len(rows), 9)
        self.assertTrue(all(0 < y < 3999 for y in rows))
        self.assertEqual(core.sample_rows(1), [0])
        self.assertEqual(core.sample_rows(0), [])

    def test_is_blank(self):
        self.assertTrue(core.is_blank([(0, 0, 0), (1.5, 0, 2)]))
        self.assertFalse(core.is_blank([(0, 0, 0), (0, 0, 40)]))
        self.assertFalse(core.is_blank([]))

    def test_corona_error_since_picks_entry_after_start(self):
        path = self.write_log()
        from datetime import datetime
        started = datetime(2026, 11, 20, 23, 34, 10).timestamp()
        text = core.corona_error_since(path, started)
        self.assertIn("ran out of RAM", text)
        self.assertNotIn("Debugging", text)
        later = datetime(2026, 11, 20, 23, 40, 0).timestamp()
        self.assertIsNone(core.corona_error_since(path, later))
        self.assertIsNone(core.corona_error_since(path.with_name("missing.txt"), started))
        self.assertIsNone(core.corona_error_since(None, started))

    def test_blank_render_message(self):
        self.assertIn("内存不足", core.blank_render_message("Bad allocation Exception! Chaos Corona ran out of RAM"))
        self.assertIn("Corona 报错", core.blank_render_message("Some other failure"))
        self.assertIn("全黑", core.blank_render_message(None))


class KeyDistanceClampTests(unittest.TestCase):
    ROOM = ((-5931.0, -7343.0, 0.0), (2631.0, 1639.0, 2800.0))
    CENTER = (444.0, 200.0, 400.0)

    def test_ray_exit_distance(self):
        self.assertAlmostEqual(core.ray_exit_distance((0, 0, 0), (1, 0, 0), (-1, -1, -1), (2, 2, 2)), 2)
        self.assertAlmostEqual(core.ray_exit_distance((0, 0, 0), (0, 0, -1), (-1, -1, -1), (2, 2, 2)), 1)
        self.assertIsNone(core.ray_exit_distance((5, 0, 0), (1, 0, 0), (-1, -1, -1), (2, 2, 2)))
        self.assertIsNone(core.ray_exit_distance((0, 0, 0), (0, 0, 0), (-1, -1, -1), (2, 2, 2)))

    def test_right_front_key_is_pulled_inside_room(self):
        direction = core.lighting_position_max(45, 35, 1.0, (0.0, 0.0, 0.0))
        clamped = core.clamp_key_distance(4000.0, self.CENTER, direction, self.ROOM, 300.0)
        self.assertLess(clamped, 4000.0)
        x = self.CENTER[0] + direction[0] * clamped
        self.assertLessEqual(x, self.ROOM[1][0] - 299.0)

    def test_key_distance_falloff(self):
        self.assertAlmostEqual(core.key_distance_falloff(2000.0, 4000.0), 0.25)
        self.assertEqual(core.key_distance_falloff(4000.0, 4000.0), 1.0)
        self.assertEqual(core.key_distance_falloff(0, 4000.0), 1.0)

    def test_left_key_keeps_distance_and_no_room_is_noop(self):
        direction = core.lighting_position_max(-45, 35, 1.0, (0.0, 0.0, 0.0))
        self.assertEqual(core.clamp_key_distance(3000.0, self.CENTER, direction, self.ROOM, 300.0), 3000.0)
        self.assertEqual(core.clamp_key_distance(3000.0, self.CENTER, direction, None, 300.0), 3000.0)


if __name__ == "__main__":
    unittest.main()
