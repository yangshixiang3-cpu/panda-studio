"""实测修正的离线回归：严格输出协议与双候选颜色匹配。"""
import copy
import json
from pathlib import Path
import sys
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.calibration import overrides_from_arrays, clamp_overrides, validate_schema
from server.color_stats import color_statistics
from fake_codex import array_overrides


class StrictSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads((ROOT/'server/calibration_schema.json').read_text(encoding='utf-8'))
        self.result = dict(verdict='adjust', summary='微调颜色。', confidence=.8, notes=[],
                           overrides=array_overrides({'materials': {'木': {}, '玻璃': {'ior': None}}}))

    def test_every_object_is_closed_and_all_properties_are_required(self):
        objects = []
        def visit(node):
            if isinstance(node, dict):
                kind = node.get('type', [])
                if kind == 'object' or isinstance(kind, list) and 'object' in kind:
                    self.assertIn('properties', node)
                    self.assertIs(node.get('additionalProperties'), False)
                    self.assertEqual(set(node.get('required', [])), set(node['properties']))
                    objects.append(node)
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)
        visit(self.schema)
        self.assertEqual(len(objects), 8)
        validate_schema(self.result, self.schema)

    def test_all_material_fields_required_and_only_ior_nullable(self):
        for key in self.result['overrides']['materials'][0]:
            value = copy.deepcopy(self.result)
            del value['overrides']['materials'][0][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_schema(value, self.schema)
        for field in ('brightness_mul', 'name'):
            value = copy.deepcopy(self.result)
            value['overrides']['materials'][0][field] = None
            with self.assertRaises(ValueError):
                validate_schema(value, self.schema)
        self.result['overrides']['materials'][1]['ior'] = 1.5
        validate_schema(self.result, self.schema)

    def test_dynamic_keys_extra_fields_and_invalid_item_number_rejected(self):
        for mutate in (
            lambda v: v['overrides'].update(materials={'木': {}}),
            lambda v: v['overrides']['lights'].update(per_light={'灯': {}}),
            lambda v: v['overrides']['environment'].update(exposure=1),
            lambda v: v['overrides'].update(items=[{'no': True, 'rotate_deg': 0}]),
            lambda v: v['overrides'].update(items=[{'no': '1', 'rotate_deg': 0}]),
        ):
            value = copy.deepcopy(self.result)
            mutate(value)
            with self.assertRaises(ValueError):
                validate_schema(value, self.schema)

    def test_array_conversion_then_original_clamp_and_name_filter(self):
        wire = array_overrides({'materials': {'木': {'brightness_mul': 9, 'ior': 1.8},
                                             '玻璃': {'ior': None}, '未知': {}},
            'lights': {'intensity_mul': 8, 'rotate_deg': -900, 'per_light': {'主灯': {'intensity_mul': 8}, '未知': {'intensity_mul': 1}}},
            'environment': {'reflection_strength_mul': 9},
            'items': {'1': {'lights': {'rotate_deg': 300}}, '2': {'lights': {'rotate_deg': 40}}}})
        original = copy.deepcopy(wire)
        self.result['overrides'] = wire
        validate_schema(self.result, self.schema)
        converted = overrides_from_arrays(wire)
        result = clamp_overrides(converted, {'materials': [{'name': '木', 'kind': 'wood'}, {'name': '玻璃', 'kind': 'glass'}],
                                             'lights': [{'name': '主灯'}]},
                                 [{'no': 1, 'view': 'main'}, {'no': 2, 'view': 'front'}])
        self.assertEqual(result['materials']['木']['brightness_mul'], 2.5)  # 上限 2026-11-20 放宽到 2.5
        self.assertNotIn('ior', result['materials']['木'])
        self.assertNotIn('未知', result['materials'])
        self.assertIsNone(result['materials']['玻璃']['ior'])
        self.assertEqual(result['lights'], {'intensity_mul': 1.6, 'rotate_deg': -90, 'per_light': {'主灯': {'intensity_mul': 2}}})
        self.assertEqual(result['items'], {'1': {'lights': {'rotate_deg': 90}}})
        self.assertEqual(result['environment'], {'reflection_strength_mul': 2})
        self.assertEqual(wire, original)

    def test_duplicate_material_light_or_item_is_rejected(self):
        for group in ('materials', 'lights', 'items'):
            value = array_overrides({})
            if group == 'materials':
                value[group] = [{'name': '重复'}, {'name': '重复'}]
            elif group == 'lights':
                value[group]['per_light'] = [{'name': '重复'}, {'name': '重复'}]
            else:
                value[group] = [{'no': 1, 'rotate_deg': 0}, {'no': 1, 'rotate_deg': 5}]
            with self.subTest(group=group), self.assertRaises(ValueError):
                overrides_from_arrays(value)


class GammaRegionTests(unittest.TestCase):
    def statistics(self, colors, legend):
        regions = Image.new('RGB', (len(colors), 1))
        regions.putdata(colors)
        return color_statistics(Image.new('RGB', regions.size, (128, 128, 128)), regions, legend)

    def test_raw_gamma_nearest_threshold_and_black(self):
        result = self.statistics([(32,128,224), (99,187,241), (224,128,32), (241,187,99), (0,0,0)],
                                 {'#2080E0': '奶油白', '#E08020': '木'})
        self.assertEqual([r['pixels'] for r in result.values()], [2, 2])
        self.assertEqual([r['area_ratio'] for r in result.values()], [.4, .4])
        result = self.statistics([(255,30,0), (255,31,0), (234,21,0)], {'#FF0000': '红'})
        self.assertEqual(result['红']['pixels'], 2)

    def test_nearest_candidate_wins_with_overlapping_radius(self):
        result = self.statistics([(100,108,100)], {'#646464': '甲', '#646E64': '乙'})
        self.assertEqual(result['甲']['pixels'], 0)
        self.assertEqual(result['乙']['pixels'], 1)

    def test_black_excluded_even_if_legend_contains_black(self):
        result = self.statistics([(0,0,0), (1,0,0)], {'#000000': '黑色材质'})
        self.assertEqual(result['黑色材质']['pixels'], 1)


if __name__ == '__main__':
    unittest.main()
