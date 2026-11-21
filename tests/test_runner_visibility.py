"""Max 状态只认用户看得见的 Max：后台隐藏启动（建模自动化）或正在关闭的 3dsmax.exe 不算「已打开」。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import runner  # noqa: E402
from server.runner import RunnerStatus  # noqa: E402

TASKLIST = (b'"3dsmax.exe","31728","Console","1","3,012,345 K"\r\n'
            b'"3dsmax.exe","40001","Console","1","2,100,000 K"\r\n')


class MaxVisibilityTest(unittest.TestCase):
    def setUp(self):
        if sys.platform != 'win32':
            self.skipTest('只在 Windows 上探测 Max 窗口')
        (Path(__file__).parent / 'tmp').mkdir(exist_ok=True)   # 刚克隆的仓库里没有 tests/tmp
        self.root = Path(tempfile.mkdtemp(prefix='runner-vis-', dir=Path(__file__).parent / 'tmp'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / 'exchange').mkdir()

    def probe(self, windows):
        done = SimpleNamespace(returncode=0, stdout=TASKLIST)
        with patch.object(runner.subprocess, 'run', return_value=done), \
             patch.object(runner, 'visible_windows', return_value=windows):
            return RunnerStatus._probe_max()

    def test_visible_and_hidden_max(self):
        running, titles, hidden = self.probe([(31728, '111.max - Autodesk 3ds Max 2025')])
        self.assertTrue(running)
        self.assertEqual(titles, ['111.max - Autodesk 3ds Max 2025'])
        self.assertEqual(hidden, 1)

    def test_only_hidden_max_is_not_open(self):
        self.assertEqual(self.probe([]), (False, [], 2))

    def test_status_reports_windows_and_allows_start_when_only_hidden(self):
        exe = self.root / '3dsmax.exe'
        exe.write_bytes(b'')
        status = RunnerStatus(self.root, self.root / 'exchange', {'max_exe': str(exe)})
        with patch.object(RunnerStatus, '_probe_max', return_value=(False, [], 2)):
            result = status.status()
        self.assertFalse(result['max_running'])
        self.assertEqual(result['max_hidden'], 2)
        self.assertTrue(result['can_start'])

    def test_old_boolean_probe_still_works(self):
        status = RunnerStatus(self.root, self.root / 'exchange', {})
        with patch.object(RunnerStatus, '_probe_max', return_value=True):
            self.assertTrue(status.status()['max_running'])


if __name__ == '__main__':
    unittest.main()
