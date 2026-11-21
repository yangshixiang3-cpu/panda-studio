"""本机设置：校验、写回 config.json、立即生效；内置白底模板；一键装 Max 连接插件。"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import max_plugin  # noqa: E402
from server.core import Studio, StudioError, TEMPLATE_FILE, atomic_json, read_json  # noqa: E402
from server.settings import MachineSettings  # noqa: E402
from test_temp_dirs import test_directory  # noqa: E402


class MachineSettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = test_directory(self, 'settings-')
        config = read_json(ROOT/'config.json')
        config.update(calibration_max_rounds=0, max_exe='', history_roots=[])
        atomic_json(self.root/'config.json', config)
        self.studio = Studio(self.root, read_json(self.root/'config.json'))
        self.addCleanup(self.studio.calibration.close)
        self.settings = MachineSettings(self.studio)
        tools = self.root/'3ds Max 2025'
        tools.mkdir()
        self.exe = tools/'3dsmax.exe'
        self.exe.write_bytes(b'')
        (self.root/TEMPLATE_FILE).parent.mkdir(parents=True)
        (self.root/TEMPLATE_FILE).write_bytes(b'')
        # 插件写到临时的「本机用户 Max 目录」，不碰真实的 Max
        self.local = self.root/'localappdata'
        patcher = patch.dict(os.environ, {'LOCALAPPDATA': str(self.local)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_empty_settings_not_ready(self):
        data = self.settings.read()
        self.assertFalse(data['ready'])
        self.assertTrue(data['checks']['template'])
        self.assertEqual(data['data_dir'], str(self.studio.exchange))
        self.assertNotIn('template_max', data)

    def test_template_is_builtin(self):
        self.assertEqual(self.studio.template_path(), (self.root/TEMPLATE_FILE).resolve())
        (self.root/TEMPLATE_FILE).unlink()
        with patch.object(self.studio.runner, '_probe_max', return_value=False):
            self.assertFalse(self.settings.save({'max_exe': str(self.exe)})['ready'])

    def test_save_validates_writes_config_and_updates_runner(self):
        with patch.object(self.studio.runner, '_probe_max', return_value=False):
            data = self.settings.save({'max_exe': str(self.exe), 'history_roots': [str(self.root)]})
            self.assertTrue(data['ready'])
            self.assertEqual(data['max_year'], 2025)
            saved = read_json(self.root/'config.json')
            self.assertEqual(saved['max_exe'], str(self.exe))
            self.assertEqual(saved['history_roots'], [str(self.root)])
            self.assertEqual(self.studio.runner.executable, self.exe.resolve())
            self.assertTrue(self.studio.runner.status()['can_start'])

    def test_rejects_wrong_files(self):
        other = self.root/'3ds Max 2025'/'notepad.exe'
        other.write_bytes(b'')
        with self.assertRaisesRegex(StudioError, '3dsmax.exe'):
            self.settings.save({'max_exe': str(other)})
        with self.assertRaisesRegex(StudioError, '找不到这个文件夹'):
            self.settings.save({'history_roots': [str(self.root/'不存在')]})
        self.assertEqual(read_json(self.root/'config.json')['max_exe'], '')

    def test_connect_max_finds_max_and_installs_plugin(self):
        user = self.local/'Autodesk'/'3dsMax'/'2025 - 64bit'
        (user/'CHS'/'scripts').mkdir(parents=True)
        (user/'ENU'/'scripts'/'startup').mkdir(parents=True)
        other = user/'CHS'/'scripts'/'startup'/'别人的.ms'
        other.parent.mkdir()
        other.write_text('-- 不动', encoding='utf-8')
        with patch('server.settings.detect_max', return_value=[str(self.exe)]), \
                patch.object(self.studio.runner, '_probe_max', return_value=False):
            data = self.settings.connect_max()
        self.assertEqual(read_json(self.root/'config.json')['max_exe'], str(self.exe))
        self.assertTrue(data['plugin']['installed'] and data['plugin']['current'])
        self.assertIn('自动连上', data['message'])
        for language in ('CHS', 'ENU'):
            text = (user/language/'scripts'/'startup'/max_plugin.PLUGIN_NAME).read_text(encoding='utf-8')
            self.assertIn(str((self.root/'max'/'启动白底执行器.ms').resolve()), text)
        self.assertEqual(other.read_text(encoding='utf-8'), '-- 不动')
        removed = self.settings.remove_plugin()
        self.assertFalse(removed['plugin']['installed'])
        self.assertFalse((user/'CHS'/'scripts'/'startup'/max_plugin.PLUGIN_NAME).exists())
        self.assertTrue(other.exists())

    def test_connect_max_without_max(self):
        with patch('server.settings.detect_max', return_value=[]):
            with self.assertRaisesRegex(StudioError, '没找到 3ds Max'):
                self.settings.connect_max()

    def test_plugin_never_overwrites_foreign_file_and_refreshes_path(self):
        startup = self.local/'Autodesk'/'3dsMax'/'2025 - 64bit'/'CHS'/'scripts'/'startup'
        startup.mkdir(parents=True)
        (startup/max_plugin.PLUGIN_NAME).write_text('-- 别人的同名脚本', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '没有覆盖'):
            max_plugin.install(self.root, self.exe)
        (startup/max_plugin.PLUGIN_NAME).unlink()
        max_plugin.install(self.root/'旧位置', self.exe)
        self.assertFalse(max_plugin.status(self.root, self.exe)['current'])
        max_plugin.refresh(self.root, self.exe)
        self.assertTrue(max_plugin.status(self.root, self.exe)['current'])

    def test_max_year(self):
        self.assertEqual(max_plugin.max_year(r'D:\3d2025\3ds Max 2025\3ds Max 2025\3dsmax.exe'), 2025)
        self.assertEqual(max_plugin.max_year(r'C:\Program Files\Autodesk\3ds Max 2024\3dsmax.exe'), 2024)
        self.assertIsNone(max_plugin.max_year(r'C:\工具\3dsmax.exe'))


if __name__ == '__main__':
    unittest.main()
