"""隔离测试目录。成功时清理，失败时保留现场。"""
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

TMP = Path(__file__).resolve().parent/'tmp'


def test_directory(case, prefix):
    TMP.mkdir(parents=True, exist_ok=True)
    root = TMP/(prefix+uuid4().hex)
    root.mkdir()
    outcome = case._outcome

    def remove_if_successful():
        result = outcome.result
        unsuccessful = result.failures + result.errors + result.unexpectedSuccesses
        for item in unsuccessful:
            target = item[0] if isinstance(item, tuple) else item
            if target is case or getattr(target, 'test_case', None) is case:
                return
        if root.resolve().parent != TMP.resolve():
            raise RuntimeError('测试目录越界。')
        shutil.rmtree(root)

    case.addCleanup(remove_if_successful)
    return root


class TemporaryDirectoryTests(unittest.TestCase):
    def test_success_removes_directory(self):
        paths = []

        class Probe(unittest.TestCase):
            def runTest(self):
                path = test_directory(self, 'probe-')
                paths.append(path)
                (path/'临时文件').touch()

        self.assertTrue(Probe().run().wasSuccessful())
        self.assertFalse(paths[0].exists())
