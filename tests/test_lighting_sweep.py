"""打光扫参脚本的离线测试。"""
from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import light_sweep as sweep
from test_temp_dirs import test_directory


class LightingSweepTests(unittest.TestCase):
    def setUp(self):
        self.directory = test_directory(self, 'light-sweep-')
        self.config = self.directory / "config.json"
        self.config.write_bytes(b'{"port": 8770, "lighting_tuning": {"fill_min": 0.2}}\r\n')

    @staticmethod
    def write_images(beauty, mask, bright=210):
        image = Image.new("L", (128, 100), 240)
        ids = Image.new("RGB", image.size, "black")
        levels, regions = ImageDraw.Draw(image), ImageDraw.Draw(ids)
        for box, level, color in (
            ((0, 24, 47, 90), bright, "red"),
            ((48, 24, 87, 90), 100, "lime"),
            ((88, 24, 103, 55), 210, "blue"),
            ((88, 0, 103, 7), 210, "blue"),
        ):
            levels.rectangle(box, fill=level)
            regions.rectangle(box, fill=color)
        image.save(beauty, "JPEG", quality=100)
        ids.save(mask, "JPEG", quality=100, subsampling=0)

    def test_dry_run_only_reads_and_validates_inputs(self):
        original = self.config.read_bytes()
        bad = self.directory / "bad.json"
        bad.write_text('[{"name":"bad","lighting":{"azimuth_deg":0},"lighting_tuning":{"fill_gamma":0}}]',
                       encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sweep, "request") as network, redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(sweep.main(["--dry-run", "--config", str(self.config)]), 0)
            self.assertEqual(sweep.main(["--dry-run", "--config", str(self.config), "--cases", str(bad)]), 1)
            network.assert_not_called()
        result = json.loads(out.getvalue())
        self.assertEqual(result["task"], sweep.DEFAULT_TASK)
        self.assertEqual(len(result["cases"]), 9)
        self.assertEqual(result["cases"][0]["lighting_tuning"], {"fill_min": .2, "capture_mask": True})
        self.assertIsNone(result["cases"][0]["lighting"]["azimuth_deg"])
        self.assertIn("fill_gamma", err.getvalue())
        self.assertEqual(self.config.read_bytes(), original)

    def test_restoration_on_failure_and_absence_preserves_other_settings(self):
        original = self.config.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "模拟扫参失败"):
            with sweep.temporary_tuning(self.config) as update:
                update({"fill_min": .1})
                self.assertEqual(sweep.read_config(self.config)["lighting_tuning"], {"fill_min": .1})
                raise RuntimeError("模拟扫参失败")
        self.assertEqual(self.config.read_bytes(), original)

        self.config.write_text('{"port":8770}', encoding="utf-8")
        with sweep.temporary_tuning(self.config) as update:
            update({"probe": True})
            self.config.write_text('{"port":8797,"lighting_tuning":{"probe":true}}', encoding="utf-8")
        self.assertEqual(sweep.read_config(self.config), {"port": 8797})

    def test_metrics_find_the_two_largest_ids_and_exclude_product_from_background(self):
        beauty, mask = (self.directory / name for name in ("beauty.jpg", "mask.jpg"))
        self.write_images(beauty, mask)
        metrics = sweep.image_metrics(beauty, mask)
        self.assertAlmostEqual(metrics["face_ratio"], 2.1, delta=.02)
        self.assertAlmostEqual(metrics["left_mean"], 210, delta=1)
        self.assertAlmostEqual(metrics["right_mean"], (100 * 134 + 210 * 640) / 774, delta=1)
        self.assertAlmostEqual(metrics["right_left_ratio"], metrics["right_mean"] / metrics["left_mean"])
        self.assertAlmostEqual(metrics["bright_face_mean"], 210, delta=2)
        self.assertAlmostEqual(metrics["dark_face_mean"], 100, delta=2)
        self.assertAlmostEqual(metrics["background_mean"], 240, delta=.5)
        self.assertAlmostEqual(metrics["product_mean"], (210 * 3856 + 100 * 2680) / 6536, delta=1)

    def test_single_id_reports_unavailable_face_ratio(self):
        beauty = self.directory / "beauty.jpg"
        mask = self.directory / "mask.jpg"
        self.write_images(beauty, mask)
        ids = Image.new("RGB", (128, 100), "black")
        ImageDraw.Draw(ids).rectangle((0, 24, 47, 90), fill="red")
        ids.save(mask, "JPEG", quality=100, subsampling=0)
        metrics = sweep.image_metrics(beauty, mask)
        self.assertNotIn("face_ratio", metrics)
        self.assertIn("两个独立", metrics["legacy_id_metric_error"])
        self.assertIn("缺少产品像素", metrics["metric_error"])
        self.assertAlmostEqual(metrics["product_mean"], 210, delta=2)

    def test_single_color_id_can_still_measure_left_and_right(self):
        beauty, mask = (self.directory / name for name in ("beauty.jpg", "mask.jpg"))
        image = Image.new("L", (120, 100), 240)
        ids = Image.new("RGB", image.size, "black")
        ImageDraw.Draw(ids).rectangle((0, 32, 119, 95), fill="red")
        ImageDraw.Draw(image).rectangle((0, 32, 39, 95), fill=70)
        ImageDraw.Draw(image).rectangle((80, 32, 119, 95), fill=140)
        image.save(beauty, "PNG")
        ids.save(mask, "PNG")
        metrics = sweep.image_metrics(beauty, mask)
        self.assertEqual(metrics["right_left_ratio"], 2)
        self.assertEqual(metrics["metric_error"], "")
        self.assertIn("两个独立", metrics["legacy_id_metric_error"])

    def test_sweep_copies_outputs_writes_csv_and_restores_config(self):
        endpoint = "/api/tasks/" + sweep.DEFAULT_TASK
        base = "http://127.0.0.1:8797"
        output = self.directory / "out"
        shots = self.directory / "smallshots"
        extra = shots / "_extra"
        extra.mkdir(parents=True)
        mask = extra / "1_遮罩_ID.jpg"
        beauty = shots / "1.jpg"
        case = sweep.make_cases(sweep.read_config(self.config))[0]
        saved = self.config.read_bytes()
        status = {"job_id": "本轮", "type": "render", "state": "done",
                  "updated_at": "2026-11-21T00:00:04+00:00",
                  "items": [{"no": 1, "state": "done", "started_at": "2026-11-21T00:00:01+00:00"}]}
        bundle = {"task": {"items": [{"no": 1, "enabled": True}], "ai_calibration": {"enabled": True}},
                  "jobs": [], "task_dir": str(self.directory)}

        def request(host, path, method="GET", data=None):
            self.assertEqual(host, base)
            if path == "/api/health":
                return {"service": "whitebg-studio", "root": str(self.directory)}
            if path == endpoint + "/jobs":
                self.assertEqual(data, {"mode": "smallshot"})
                tuning = sweep.read_config(self.config)["lighting_tuning"]
                self.assertEqual(tuning, case["lighting_tuning"])
                self.write_images(beauty, mask)
                bundle["jobs"] = [status]
                return {"job": {"job_id": "本轮", "mode": "smallshot", "lighting_tuning": tuning,
                                "items": [{"out_image": str(beauty)}]}}
            self.assertEqual(path, endpoint)
            return bundle

        args = SimpleNamespace(config=self.config, timeout=1)
        with mock.patch.object(sweep, "request", side_effect=request) as api, mock.patch.object(
                sweep.subprocess, "run") as contact, redirect_stdout(io.StringIO()):
            self.assertEqual(sweep.run_sweep(args, [case], base, endpoint, output), 0)
        api.assert_any_call(base, endpoint, "PUT", {"ai_calibration": {"enabled": False}})
        api.assert_any_call(base, endpoint, "PUT", {"ai_calibration": {"enabled": True}})
        self.assertEqual(self.config.read_bytes(), saved)
        self.assertEqual((output / "renders" / "01-template.jpg").read_bytes(), beauty.read_bytes())
        self.assertEqual((output / "masks" / "01-template_遮罩_ID.jpg").read_bytes(), mask.read_bytes())
        with (output / "metrics.csv").open(encoding="utf-8-sig", newline="") as stream:
            row, = csv.DictReader(stream)
        self.assertEqual(row["job_id"], "本轮")
        self.assertEqual(float(row["render_seconds"]), 3)
        self.assertAlmostEqual(float(row["face_ratio"]), 2.1, delta=.02)
        self.assertGreater(float(row["right_left_ratio"]), 0)
        self.assertIn(str(output / "contact-sheet.jpg"), contact.call_args.args[0])
