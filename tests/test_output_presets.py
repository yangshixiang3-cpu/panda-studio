"""正式图输出档位 4K / 6K / 8K。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.core import OUTPUT_PRESETS, Studio, StudioError, read_json  # noqa: E402
from test_temp_dirs import test_directory  # noqa: E402


class OutputPresetTests(unittest.TestCase):
    def setUp(self):
        self.root = test_directory(self, 'presets-')
        config = read_json(ROOT/'config.json')
        config.update(calibration_max_rounds=0, smallshot_size={'portrait': [60, 80], 'landscape': [80, 60]})
        config.pop('output_presets', None)
        self.studio = Studio(self.root, config)
        self.addCleanup(self.studio.calibration.close)
        product = self.root/'产品'
        product.mkdir()
        (product/'产品.max').touch()
        self.id = self.studio.create({'product_dir': str(product)})['id']

    def job_sizes(self):
        job = self.studio.submit(self.id, {'mode': 'final'})
        return {item['no']: (item['width'], item['height']) for item in job['items']}

    def test_presets_match_user_definition(self):
        self.assertEqual(OUTPUT_PRESETS['4K']['portrait'], [3000, 4000])
        self.assertEqual(OUTPUT_PRESETS['6K']['portrait'], [4500, 6000])
        self.assertEqual(OUTPUT_PRESETS['8K']['portrait'], [6000, 8000])
        for sizes in OUTPUT_PRESETS.values():
            self.assertEqual(sizes['landscape'], sizes['portrait'][::-1])

    def test_default_uses_final_size_and_payload_lists_presets(self):
        payload = self.studio.merge(self.id)
        self.assertEqual(payload['output_preset'], '4K')
        self.assertEqual(set(payload['output_presets']), {'4K', '6K', '8K'})
        sizes = self.job_sizes()
        for item in self.studio.merge(self.id)['task']['items']:
            if item['no'] in sizes:
                self.assertEqual(sizes[item['no']], tuple(self.studio.config['final_size'][item['orientation']]))

    def test_8k_preset_drives_final_job_and_resets_finished_finals(self):
        task = self.studio.merge(self.id)['task']
        for item in task['items']:
            item['final'].update(state='done', job_id='old')
        self.studio.save(task)
        task = self.studio.update(self.id, {'output_preset': '8K'})
        self.assertEqual(task['output_preset'], '8K')
        self.assertTrue(all(item['final']['state'] == 'none' for item in task['items']))
        sizes = self.job_sizes()
        task = self.studio.merge(self.id)['task']
        for item in task['items']:
            if item.get('enabled', True):
                self.assertEqual(sizes[item['no']], tuple(OUTPUT_PRESETS['8K'][item['orientation']]))

    def test_invalid_preset_and_change_during_render_rejected(self):
        with self.assertRaisesRegex(StudioError, '输出尺寸无效'):
            self.studio.update(self.id, {'output_preset': '12K'})
        self.studio.submit(self.id, {'mode': 'final'})
        with self.assertRaisesRegex(StudioError, '渲染结束后再改输出尺寸'):
            self.studio.update(self.id, {'output_preset': '6K'})


if __name__ == '__main__':
    unittest.main()
