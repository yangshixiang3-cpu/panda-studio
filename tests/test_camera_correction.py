"""测试独立相机设置的校验、持久化和作业快照。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.core import Studio, StudioError, draft_items, read_json, validate_camera
from test_temp_dirs import test_directory


class CameraCorrectionTests(unittest.TestCase):
    def test_default_requires_boolean_when_supplied(self):
        camera = draft_items()[0]["camera"]
        self.assertIs(camera.pop("vertical_correction"), False)
        self.assertIs(validate_camera(camera)["vertical_correction"], False)
        self.assertIs(validate_camera(dict(camera, vertical_correction=True))["vertical_correction"], True)
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(StudioError):
                validate_camera(dict(camera, vertical_correction=value))

    def test_setting_survives_item_save_and_job_snapshot_and_resets_on_draft(self):
        root = test_directory(self, "camera-correction-")
        studio = Studio(root, read_json(ROOT / "config.json"))
        self.addCleanup(studio.calibration.close)
        product = root / "产品"
        product.mkdir()
        (product / "产品.max").touch()
        task = studio.create({"product_dir": str(product)})
        task["items"][0]["camera"]["vertical_correction"] = True
        studio.update(task["id"], {"items": task["items"]})
        saved = studio.load(task["id"])
        self.assertEqual([row["camera"]["vertical_correction"] for row in saved["items"]],
                         [True])
        job = studio.submit(task["id"], {"mode": "smallshot"})
        self.assertEqual([row["camera"]["vertical_correction"] for row in job["items"]],
                         [True])
        other = studio.create({"product_dir": str(product)})
        other["items"][0]["camera"]["vertical_correction"] = True
        studio.update(other["id"], {"items": other["items"]})
        reset = studio.draft(other["id"], {"size_mm": [1200, 850, 400]})
        self.assertTrue(all(row["camera"]["vertical_correction"] is False for row in reset["items"]))
