"""删除任务：移到回收站（可撤销）、出图中不能删、回收站过期清理。"""
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.core import Studio, StudioError, read_json
from test_temp_dirs import test_directory


class TaskDeleteTests(unittest.TestCase):
    def setUp(self):
        root = test_directory(self, "task-delete-")
        self.studio = Studio(root, read_json(ROOT / "config.json"))
        self.addCleanup(self.studio.calibration.close)
        product = root / "产品"
        product.mkdir()
        (product / "产品.max").touch()
        self.product = product
        self.task = self.studio.create({"product_dir": str(product)})

    def ids(self):
        return [t["id"] for t in self.studio.list_tasks(str(self.product))]

    def test_delete_moves_to_trash_and_restore_brings_it_back(self):
        task_id = self.task["id"]
        result = self.studio.delete_task(task_id)
        self.assertEqual(result["deleted"], task_id)
        self.assertNotIn(task_id, self.ids())
        self.assertTrue((self.studio.exchange / "trash" / task_id / "task.json").is_file())
        with self.assertRaises(StudioError):
            self.studio.load(task_id)
        restored = self.studio.restore_task(task_id)
        self.assertEqual(restored["task"]["id"], task_id)
        self.assertIn(task_id, self.ids())
        with self.assertRaises(StudioError) as missing:
            self.studio.restore_task(task_id)
        self.assertEqual(missing.exception.status, 404)

    def test_cannot_delete_while_rendering(self):
        self.studio.submit(self.task["id"], {"mode": "smallshot"})
        with self.assertRaises(StudioError) as error:
            self.studio.delete_task(self.task["id"])
        self.assertEqual(error.exception.status, 409)
        self.assertIn(self.task["id"], self.ids())

    def test_old_trash_is_purged(self):
        task_id = self.task["id"]
        self.studio.delete_task(task_id)
        entry = self.studio.exchange / "trash" / task_id
        old = time.time() - (self.studio.TRASH_KEEP_DAYS + 1) * 86400
        os.utime(entry, (old, old))
        self.assertEqual(self.studio.purge_trash(), 1)
        self.assertFalse(entry.exists())


if __name__ == "__main__":
    unittest.main()
