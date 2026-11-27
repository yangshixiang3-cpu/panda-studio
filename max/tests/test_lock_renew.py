"""Max 锁文件续期纯逻辑与后台守护线程测试（不依赖 3ds Max / pymxs）。"""
import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

MAX_DIR = Path(__file__).resolve().parents[1]
TMP_DIR = MAX_DIR.parent / "tests" / "tmp"
sys.path.insert(0, str(MAX_DIR))
import wb_core as core


class LockFileHelpersTests(unittest.TestCase):
    def setUp(self):
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        self.dir = TMP_DIR / ("lock-renew-" + uuid4().hex[:8])
        self.dir.mkdir(parents=True)
        self.lock_path = self.dir / core.LOCK_FILE_NAME

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_write_read_and_format_roundtrip(self):
        written = core.write_lock_file(
            self.lock_path, "WhiteBG:20261126-101010-abcd", r"D:\tasks\t1",
            started="2026-11-26 10:10:10", now_epoch=1700000000, stamp="2026-11-26 10:10:10",
        )
        loaded = core.read_lock_file(self.lock_path)
        self.assertEqual(loaded, written)
        text = self.lock_path.read_text(encoding="utf-8")
        self.assertIn("owner=WhiteBG:20261126-101010-abcd\n", text)
        self.assertIn("epoch=1700000000\n", text)

    def test_renew_only_updates_when_owner_matches(self):
        core.write_lock_file(
            self.lock_path, "WhiteBG:job1", "task1",
            started="2026-11-26 10:00:00", now_epoch=1700000000, stamp="2026-11-26 10:00:00",
        )
        # 相同 owner：刷新 epoch 和 stamp，保留 task 和 started
        self.assertTrue(core.renew_lock_file(self.lock_path, "WhiteBG:job1", now_epoch=1700000180, stamp="2026-11-26 10:03:00"))
        record = core.read_lock_file(self.lock_path)
        self.assertEqual(record["owner"], "WhiteBG:job1")
        self.assertEqual(record["task"], "task1")
        self.assertEqual(record["started"], "2026-11-26 10:00:00")
        self.assertEqual(record["epoch"], 1700000180)
        self.assertEqual(record["stamp"], "2026-11-26 10:03:00")

        # 他人占用：拒绝刷新且不覆盖文件内容
        self.assertFalse(core.renew_lock_file(self.lock_path, "Codex:other", now_epoch=1700000999))
        self.assertEqual(core.read_lock_file(self.lock_path)["epoch"], 1700000180)
        self.assertEqual(core.read_lock_file(self.lock_path)["owner"], "WhiteBG:job1")

        # 缺失文件：返回 False，不新建文件
        self.lock_path.unlink()
        self.assertFalse(core.renew_lock_file(self.lock_path, "WhiteBG:job1", now_epoch=1700000999))
        self.assertFalse(self.lock_path.exists())

    def test_release_only_deletes_matching_owner(self):
        core.write_lock_file(self.lock_path, "WhiteBG:job1", "t1", now_epoch=1700000000)
        self.assertFalse(core.release_lock_file(self.lock_path, "Codex:other"))
        self.assertTrue(self.lock_path.is_file())
        self.assertTrue(core.release_lock_file(self.lock_path, "WhiteBG:job1"))
        self.assertFalse(self.lock_path.exists())

    def test_parse_rejects_malformed_content(self):
        for bad in ("", "no_equals", "owner=a\nepoch=notint\n", "epoch=123\n"):
            self.assertIsNone(core.parse_lock_file(bad))

    def test_lock_renewer_daemon_refreshes_lock_and_heartbeat_and_stops_cleanly(self):
        core.write_lock_file(self.lock_path, "WhiteBG:job1", "t1", now_epoch=1000)
        beats = []
        hb = mock.Mock()
        hb.beat.side_effect = lambda: beats.append(time.monotonic())
        errors = []

        with core.LockRenewer(self.lock_path, "WhiteBG:job1", interval=0.02, heartbeat=hb, on_error=errors.append) as renewer:
            self.assertIsNotNone(renewer._thread)
            self.assertTrue(renewer._thread.daemon)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and core.read_lock_file(self.lock_path)["epoch"] == 1000:
                time.sleep(0.01)
            self.assertGreater(core.read_lock_file(self.lock_path)["epoch"], 1000)
            self.assertGreaterEqual(len(beats), 1)

        self.assertIsNone(renewer._thread)
        self.assertEqual(errors, [])

        # 异常退出同样在 finally 中干净停止线程
        with self.assertRaises(RuntimeError):
            with core.LockRenewer(self.lock_path, "WhiteBG:job1", interval=0.02) as renewer2:
                thread_ref = renewer2._thread
                raise RuntimeError("模拟渲染失败")
        self.assertFalse(thread_ref.is_alive())

    def test_runner_prefers_bundled_lock_script_and_wraps_render_with_renewer(self):
        fake_pymxs = types.ModuleType("pymxs")
        runtime = mock.Mock()
        fake_pymxs.runtime = runtime
        fake_pymxs.byref = lambda value: value
        fake_qt = types.ModuleType("PySide6")
        fake_qt.QtCore, fake_qt.QtWidgets = mock.Mock(), mock.Mock()
        spec = importlib.util.spec_from_file_location("_whitebg_runner_lock_test", MAX_DIR / "wb_runner.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"pymxs": fake_pymxs, "PySide6": fake_qt}):
            spec.loader.exec_module(module)

        # 内置 max/pf_max_lock.ms 优先
        self.assertEqual(Path(module.LOCK_SCRIPT).resolve(), (MAX_DIR / "pf_max_lock.ms").resolve())

        # _render_item 在 rt.render 期间启动续期线程，即使 render 抛错也关闭
        runner = object.__new__(module.Runner)
        runner.log = mock.Mock()
        runner.log_file = mock.Mock()
        runner.flush = mock.Mock()
        runner.exchange = self.dir
        runner.owner = "WhiteBG:job9"
        runner.heartbeat = mock.Mock()
        runner.config = {"smallshot_time_limit_sec": 25}
        runner.job = {"mode": "smallshot", "task_id": "t1"}
        runner._camera = mock.Mock(return_value=(object(), object()))
        runner._output = mock.Mock(return_value=str(self.dir / "1.jpg"))
        runner._mask_setup = mock.Mock(return_value=None)
        runner._touch = mock.Mock()

        active_during_render = []

        def fake_render(**kwargs):
            active_during_render.append(any(t.name == "白底锁续期" and t.is_alive() for t in threading.enumerate()))
            raise RuntimeError("模拟中断")

        runtime.render.side_effect = fake_render
        with self.assertRaisesRegex(RuntimeError, "模拟中断"):
            runner._render_item({"no": 1, "width": 100, "height": 100, "out_image": str(self.dir / "1.jpg")}, (0, 0, 0), 1)
        self.assertEqual(active_during_render, [True])
        self.assertFalse(any(t.name == "白底锁续期" and t.is_alive() for t in threading.enumerate()))

    def test_maxscript_lock_has_no_chained_call_dot_and_release_preserves_acquire_error(self):
        import re
        for ms_file in sorted(MAX_DIR.glob("*.ms")):
            text = ms_file.read_text(encoding="utf-8-sig")
            # MAXScript 不支持 foo().bar 链式成员访问，会报「语法错误: 位于 .，需要 <因子>」
            self.assertIsNone(
                re.search(r'\(\)\.[A-Za-z_]', text),
                f"{ms_file.name} 包含 MAXScript 不支持的 (). 链式调用",
            )

        fake_pymxs = types.ModuleType("pymxs")
        runtime = mock.Mock()
        fake_pymxs.runtime = runtime
        fake_pymxs.byref = lambda value: value
        fake_qt = types.ModuleType("PySide6")
        fake_qt.QtCore, fake_qt.QtWidgets = mock.Mock(), mock.Mock()
        spec = importlib.util.spec_from_file_location("_whitebg_runner_rel_test", MAX_DIR / "wb_runner.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"pymxs": fake_pymxs, "PySide6": fake_qt}):
            spec.loader.exec_module(module)

        import json
        jobs_dir = self.dir / "jobs"
        jobs_dir.mkdir(parents=True)
        job = {
            "job_id": "20261127-104000-abcd",
            "task_id": "20261127-104000-0001",
            "type": "render",
            "mode": "smallshot",
            "created_at": "2026-11-27T10:40:00+09:00",
            "items": [],
        }
        (jobs_dir / "20261127-104000-abcd.json").write_text(json.dumps(job), encoding="utf-8")
        runner = object.__new__(module.Runner)
        runner.exchange = self.dir
        runner.config = {"max_lock_owner_prefix": "WhiteBG"}
        runner.heartbeat = None
        runner.job = runner.status = runner.status_path = runner.owner = None

        calls = []
        def fake_exec(script):
            calls.append(script)
            if len(calls) == 1:
                raise RuntimeError("模拟 acquire 失败")
            return "OK 未加载锁脚本"

        runtime.execute.side_effect = fake_exec
        status = runner.process_next()
        self.assertEqual(status["state"], "failed")
        self.assertIn("模拟 acquire 失败", status["message"])
        self.assertIn("::PFMaxLock == undefined", calls[1])


if __name__ == "__main__":
    unittest.main()
