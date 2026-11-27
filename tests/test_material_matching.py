"""精准对应参考材质、参考框保底、参考映射表与可编辑材质参数（Contract 2）单元测试。"""
from __future__ import annotations

import io
import json
import sys
import time
import unittest
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _entry in (str(TESTS_DIR), str(ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from fixture_files import needs_product_fbx  # noqa: E402
from server.calibration import (  # noqa: E402
    analyze_round,
    clamp_overrides,
    match_task_region,
    select_images,
    snapshot_task_refs,
    task_ref_candidates,
)
from server.color_stats import srgb_to_lab  # noqa: E402
from server.core import Studio, StudioError, atomic_json, read_json  # noqa: E402
from server.materials import (  # noqa: E402
    effective_overrides,
    scene_materials_detail,
    update_task_materials,
)
from server.ref_library import (  # noqa: E402
    RefLibrary,
    core_material_name,
    effective_material_ref,
    empty_library,
    material_key,
)
from test_temp_dirs import test_directory  # noqa: E402
from tools.fake_runner import run_once  # noqa: E402


def make_png_bytes(rgb=(160, 110, 70), size=(128, 128), border_white=False):
    img = Image.new('RGB', size, (255, 255, 255) if border_white else rgb)
    if border_white:
        fg = Image.new('RGB', (size[0] // 2, size[1] // 2), rgb)
        img.paste(fg, (size[0] // 4, size[1] // 4))
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def region(material, source='library', box=None):
    return {'material': material, 'material_source': source, 'box': box or [0.1, 0.1, 0.5, 0.5]}


class MaterialKeyMatchingTests(unittest.TestCase):
    def setUp(self):
        self.library = empty_library()

    def test_exact_prefix_suffix_short_core_and_synonyms(self):
        cases = [
            # 原名精确匹配与大小写
            ('摩卡红橡', '摩卡红橡'),
            ('胡桃木', '胡桃木'),
            ('奶油白', '奶油白'),
            ('北欧橡木', '北欧橡木'),
            ('幻影灰', '幻影灰'),
            ('纯黑', '纯黑'),
            ('长虹玻璃', '长虹玻璃'),
            # 短名与核心材质词反向包含
            ('胡桃', '胡桃木'),
            ('红橡', '摩卡红橡'),
            ('橡木', '北欧橡木'),
            ('长虹', '长虹玻璃'),
            # 建模常见前后缀、编号、部件名与工艺词剥除
            ('M_摩卡红橡_01', '摩卡红橡'),
            ('M01_胡桃_v2', '胡桃木'),
            ('柜体-奶油白', '奶油白'),
            ('门板_北欧橡木_木饰面', '北欧橡木'),
            ('MTL_02_幻影灰_copy', '幻影灰'),
            ('VrayMtl_纯黑拉手_03', '纯黑'),
            ('柜门-长虹_01', '长虹玻璃'),
            # 安全同义词兜底映射
            ('焦糖胡桃', '胡桃木'),
            ('黑胡桃', '胡桃木'),
            ('北美胡桃木皮', '胡桃木'),
            ('M_暖白_01', '奶油白'),
            ('柜门-奶白肤感', '奶油白'),
            ('原木柜体', '北欧橡木'),
            ('白蜡木饰面', '北欧橡木'),
            ('深灰侧板', '幻影灰'),
            ('高级灰烤漆', '幻影灰'),
            ('哑光黑脚架', '纯黑'),
            ('拉丝黑五金', '纯黑'),
        ]
        for scene_name, expected in cases:
            with self.subTest(scene_name=scene_name):
                self.assertEqual(material_key(scene_name, self.library), expected)

    def test_reverse_core_containment_into_custom_group_name(self):
        lib = empty_library()
        lib['materials']['北美白橡木'] = {'keywords': ['北美白橡木'], 'images': []}
        self.assertEqual(core_material_name('M_白橡木_02'), '白橡木')
        self.assertEqual(material_key('M_白橡木_02', lib), '北美白橡木')
        # 原名精确匹配优先级最高
        lib['materials']['胡桃'] = {'keywords': ['特殊胡桃'], 'images': []}
        self.assertEqual(material_key('胡桃', lib), '胡桃')

    def test_ordinary_glass_never_matches_fluted_glass(self):
        ordinary_glasses = [
            '玻璃', '清玻', '茶玻', '灰玻', '黑玻', '白玻', '超白玻璃', '钢化玻璃',
            '普通玻璃', '透明玻璃', '磨砂玻璃', '门板玻璃', 'M_玻璃_01', '柜门_茶玻_02', 'Glass_01',
        ]
        for name in ordinary_glasses:
            with self.subTest(name=name):
                self.assertIsNone(material_key(name, self.library))
        for fluted in ('长虹玻璃', '长虹玻', '长虹', 'M_长虹_01', '柜门-长虹玻璃'):
            with self.subTest(fluted=fluted):
                self.assertEqual(material_key(fluted, self.library), '长虹玻璃')

    def test_generic_max_names_do_not_false_match(self):
        for generic in ('板材01', 'Material #25', '01 - Default', 'CoronaPhysicalMtl', '金属', '五金', '木纹', ''):
            with self.subTest(generic=generic):
                self.assertIsNone(material_key(generic, self.library))

    def test_effective_material_ref_with_bindings(self):
        bindings = {
            '板材01': '摩卡红橡',
            'Material #25': '奶油白',
            '柜门胡桃木': '__none__',
            '柜体胡桃': '北欧橡木',
        }
        self.assertEqual(effective_material_ref('板材01', self.library, bindings), '摩卡红橡')
        self.assertEqual(effective_material_ref('Material #25', self.library, bindings), '奶油白')
        self.assertIsNone(effective_material_ref('柜门胡桃木', self.library, bindings))
        self.assertEqual(effective_material_ref('柜体胡桃', self.library, bindings), '北欧橡木')
        self.assertEqual(effective_material_ref('M_深灰_01', self.library, bindings), '幻影灰')


class RegionPreservationAndReferenceMapTests(unittest.TestCase):
    def setUp(self):
        self.root = test_directory(self, 'mat-match-')
        config = read_json(ROOT / 'config.json')
        config.update(
            exchange_dir='exchange',
            calibration_auto=False,
            codex_command=[sys.executable, str(ROOT / 'tests' / 'fake_codex.py')],
            smallshot_size={'portrait': [80, 80], 'landscape': [80, 80]},
            final_size={'portrait': [80, 80], 'landscape': [80, 80]},
        )
        self.studio = Studio(self.root, config)
        self.addCleanup(self.studio.calibration.close)
        self.refs: RefLibrary = self.studio.refs
        self.product = self.root / '测试餐边柜'
        self.product.mkdir()
        (self.product / '餐边柜.max').write_bytes(b'placeholder')
        self.task = self.studio.create({'product_dir': str(self.product)})
        self.task_id = self.task['id']
        self.folder = self.studio.task_dir(self.task_id)

    def _upload_task_ref(self, name, regions, rgb=(150, 95, 55)):
        entry = self.studio.calibration.upload_ref(self.task_id, name, make_png_bytes(rgb))
        return self.studio.calibration.change_ref(self.task_id, entry['id'], {'regions': regions})['entry']

    def test_match_task_region_bindings_clean_containment_and_no_silent_drop(self):
        library = self.refs.read()
        scene_names = ['板材01', 'M_黄铜拉手_01', '长虹玻璃门', '门板透明玻璃', '柜门胡桃']
        bindings = {'板材01': '摩卡红橡', '柜门胡桃': '__none__'}

        # 1) 反向查找 material_bindings
        self.assertEqual(
            match_task_region(region('摩卡红橡', 'library'), scene_names, library, bindings),
            ['板材01'],
        )
        # 2) __none__ 解绑的材质不会被动吃进参考库胡桃木框
        self.assertEqual(
            match_task_region(region('胡桃木', 'library'), scene_names, library, bindings),
            [],
        )
        # 3) 清洗前后缀后的双向包含匹配，且普通玻璃绝不误配长虹玻璃
        self.assertEqual(
            match_task_region(region('柜门-黄铜拉手', 'new'), scene_names, library, bindings),
            ['M_黄铜拉手_01'],
        )
        self.assertEqual(
            match_task_region(region('玻璃', 'new'), scene_names, library, bindings),
            ['门板透明玻璃'],
        )

        # 4) 未命中当前场景材质的 scene / library / new 三类框均保底归入「其他参考（<name>）」绝不丢图
        atomic_json(
            self.folder / 'preview' / 'scene_manifest.json',
            {'materials': [{'name': '旧场景材质', 'kind': 'wood'}, {'name': '板材01', 'kind': 'wood'}]},
        )
        ref_entry = self._upload_task_ref(
            '多来源未命中参考.png',
            [
                region('旧场景材质', 'scene', [0.05, 0.1, 0.2, 0.4]),
                region('幻影灰', 'library', [0.35, 0.1, 0.2, 0.4]),
                region('打样青色', 'new', [0.65, 0.1, 0.2, 0.4]),
            ],
        )
        task = self.studio.load(self.task_id)
        snapshot_task_refs(self.folder, 1, task['ai_calibration']['task_refs'])
        # 正式渲染时旧场景材质已改名为「板材01」，三框均未命中当前材质列表
        candidates = task_ref_candidates(self.folder / 'calibration' / 'round-1' / 'task_refs', ['板材01'], library)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(
            [item[3] for item in candidates],
            ['其他参考（旧场景材质）', '其他参考（幻影灰）', '其他参考（打样青色）'],
        )
        images, descriptions = select_images(
            self.studio, task, {'n': 1}, {'materials': [{'name': '板材01'}]}, {'items': []}
        )
        self.assertEqual(
            [d.get('task_ref') for d in descriptions],
            [{'id': ref_entry['id'], 'region_index': i} for i in range(3)],
        )
        for expected_label, desc in zip(('其他参考（旧场景材质）', '其他参考（幻影灰）', '其他参考（打样青色）'), descriptions):
            self.assertIn(expected_label, desc['role'])
            self.assertIn('Lab≈(', desc['role'])

    def test_sku_images_yield_to_material_references_under_twelve_image_cap(self):
        sku_dir = self.product / 'sku'
        sku_dir.mkdir()
        for idx in range(3):
            Image.new('RGB', (40, 40), (200, 180, 160)).save(sku_dir / f'历史白底_{idx + 1}.jpg')

        # 准备 5 个匹配材质，每个材质各 2 张已认可参考图 = 10 张材质参考 + 1 小样 + 1 分区图 = 12 张
        library = self.refs.read()
        scene_keys = ['摩卡红橡', '胡桃木', '奶油白', '北欧橡木', '幻影灰']
        for key in scene_keys:
            library['materials'][key]['images'] = []
            for idx in range(2):
                added = self.refs.add(key, f'{key}-{idx}.png', make_png_bytes((90 + idx * 20, 70, 50)), library)
                added['status'] = 'approved'
        library['style'] = []
        self.refs.save(library)

        round_dir = self.folder / 'calibration' / 'round-0'
        (round_dir / 'regions').mkdir(parents=True, exist_ok=True)
        sample = round_dir / '1.jpg'
        reg_img = round_dir / 'regions' / '1.png'
        Image.new('RGB', (40, 40), (140, 100, 70)).save(sample)
        Image.new('RGB', (40, 40), (255, 0, 0)).save(reg_img)

        task = self.studio.load(self.task_id)
        manifest = {'materials': [{'name': f'M_{key}_01', 'kind': 'wood'} for key in scene_keys]}
        job = {'calibration_dir': str(round_dir), 'items': [{'no': 1, 'out_image': str(sample)}]}

        images, descriptions = select_images(self.studio, task, {'n': 0}, manifest, job)
        self.assertEqual(len(images), 12)
        self.assertFalse(any('本产品已有白底' in d['role'] for d in descriptions))
        mat_roles = [d['role'] for d in descriptions[2:]]
        self.assertEqual(len(mat_roles), 10)
        for key in scene_keys:
            self.assertTrue(any(f'对应场景材质「M_{key}_01」（参考材质：{key}' in r for r in mat_roles))

        # 当只有 1 个场景材质时（2 张材质参考 + 1 小样 + 1 分区 = 4 张），剩余槽位正常补充 3 张 sku_images
        sparse_manifest = {'materials': [{'name': 'M_摩卡红橡_01', 'kind': 'wood'}]}
        images2, descriptions2 = select_images(self.studio, task, {'n': 0}, sparse_manifest, job)
        self.assertEqual(sum('本产品已有白底' in d['role'] for d in descriptions2), 3)
        # 且材质参考排在 sku_images 之前
        first_sku_idx = next(i for i, d in enumerate(descriptions2) if '本产品已有白底' in d['role'])
        last_mat_idx = max(i for i, d in enumerate(descriptions2) if '对应场景材质' in d['role'])
        self.assertLess(last_mat_idx, first_sku_idx)

    def test_analyze_round_generates_material_reference_map_and_strict_rules(self):
        # 配置四类参考来源 + 一个无参考玻璃材质 + 一个 __none__ 解绑材质
        library = self.refs.read()
        for key in library['materials']:
            library['materials'][key]['images'] = []
        library['annotated'] = []
        library['style'] = []
        approved_oak = self.refs.add('北欧橡木', '白橡已认可.png', make_png_bytes((195, 165, 125), border_white=True), library)
        approved_oak['status'] = 'approved'
        cand_black = self.refs.add('纯黑', '黑砂候选.png', make_png_bytes((35, 35, 35), border_white=True), library)
        cand_black['status'] = 'candidate'
        self.refs.save(library)

        # 参考库标注参考图：摩卡红橡
        self.refs.save_annotation(
            [{'material': '摩卡红橡', 'box': [0.1, 0.1, 0.8, 0.8]}],
            source='红橡标注库图.png',
            content=make_png_bytes((135, 78, 48)),
        )

        # 本任务参考框：胡桃木 -> 对应 M_胡桃_01
        self._upload_task_ref('任务胡桃参考.png', [region('胡桃木', 'library', [0.1, 0.1, 0.8, 0.8])], rgb=(92, 60, 42))

        # 显式绑定：板材01 -> 摩卡红橡，装饰木条 -> __none__
        update_task_materials(
            self.studio,
            self.task_id,
            {
                'action': 'set',
                'material_bindings': {'板材01': '摩卡红橡', '装饰木条': '__none__'},
            },
        )

        source_dir = self.folder / 'jobs_cal' / 'round-0'
        (source_dir / 'regions').mkdir(parents=True, exist_ok=True)
        manifest = {
            'materials': [
                {'name': 'M_胡桃_01', 'kind': 'wood'},
                {'name': '板材01', 'kind': 'wood'},
                {'name': '柜门-原木', 'kind': 'wood'},
                {'name': '拉丝黑拉手', 'kind': 'metal'},
                {'name': '清玻层板', 'kind': 'glass'},
                {'name': '装饰木条', 'kind': 'wood'},
            ],
            'lights': [{'name': '主灯'}],
            'environment': {'reflect_map': 'studio.hdr'},
        }
        legend = {
            '#FF0000': 'M_胡桃_01',
            '#00FF00': '板材01',
            '#0000FF': '柜门-原木',
            '#FFFF00': '拉丝黑拉手',
            '#00FFFF': '清玻层板',
            '#FF00FF': '装饰木条',
        }
        atomic_json(source_dir / 'scene_manifest.json', manifest)
        atomic_json(source_dir / 'regions' / 'legend.json', legend)

        sample_img = Image.new('RGB', (6, 1))
        sample_img.putdata([(80, 50, 35), (120, 70, 45), (190, 160, 120), (30, 30, 30), (220, 225, 225), (110, 80, 60)])
        region_img = Image.new('RGB', (6, 1))
        region_img.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255)])
        sample_path = source_dir / '1.png'
        region_path = source_dir / 'regions' / '1.png'
        sample_img.save(sample_path)
        region_img.save(region_path)

        task = self.studio.load(self.task_id)
        record = self.studio.calibration.record(0, 'job-r0', None)
        task['ai_calibration']['rounds'].append(record)
        self.studio.save(task)
        snapshot_task_refs(self.folder, 0, task['ai_calibration']['task_refs'])

        job = {'job_id': 'job-r0', 'calibration_dir': str(source_dir), 'items': [{'no': 1, 'out_image': str(sample_path)}]}
        analyze_round(self.studio, task, record, job)

        round_folder = self.folder / 'calibration' / 'round-0'
        payload = read_json(round_folder / 'input.json')
        prompt = (round_folder / 'prompt.txt').read_text(encoding='utf-8')

        self.assertIn('material_reference_map', payload)
        by_mat = {entry['scene_material']: entry for entry in payload['material_reference_map']}
        self.assertEqual(set(by_mat), {m['name'] for m in manifest['materials']})

        self.assertEqual(by_mat['M_胡桃_01']['reference_source'], 'task_ref')
        self.assertEqual(by_mat['M_胡桃_01']['reference_material'], '胡桃木')
        self.assertEqual(len(by_mat['M_胡桃_01']['reference_files']), 1)
        self.assertIsNotNone(by_mat['M_胡桃_01']['sample_lab'])
        self.assertIsNotNone(by_mat['M_胡桃_01']['target_lab'])

        self.assertEqual(by_mat['板材01']['reference_source'], 'library_annotated')
        self.assertEqual(by_mat['板材01']['reference_material'], '摩卡红橡')
        self.assertEqual(len(by_mat['板材01']['reference_files']), 1)
        self.assertIsNotNone(by_mat['板材01']['target_lab'])

        self.assertEqual(by_mat['柜门-原木']['reference_source'], 'library_approved')
        self.assertEqual(by_mat['柜门-原木']['reference_material'], '北欧橡木')
        # 前景平均 Lab 排除纯白背景后 L 应明显低于纯白 100
        expected_fg_l = srgb_to_lab((195, 165, 125))[0]
        self.assertAlmostEqual(by_mat['柜门-原木']['target_lab'][0], expected_fg_l, delta=2.0)

        self.assertEqual(by_mat['拉丝黑拉手']['reference_source'], 'library_candidate')
        self.assertEqual(by_mat['拉丝黑拉手']['reference_material'], '纯黑')

        for unmatched_name in ('清玻层板', '装饰木条'):
            self.assertEqual(by_mat[unmatched_name]['reference_source'], 'unmatched')
            self.assertIsNone(by_mat[unmatched_name]['reference_material'])
            self.assertEqual(by_mat[unmatched_name]['reference_files'], [])
            self.assertIsNone(by_mat[unmatched_name]['target_lab'])

        self.assertIn('material_reference_map', prompt)
        self.assertIn('严禁把 A 材质的参考串用到 B 材质', prompt)
        self.assertIn('unmatched', prompt)
        self.assertIn('环境反射[0,2]', prompt)


@needs_product_fbx
class EditableMaterialsAndCalibrationLinkageTests(unittest.TestCase):
    def setUp(self):
        self.root = test_directory(self, 'mat-edit-')
        config = read_json(ROOT / 'config.json')
        config.update(
            exchange_dir='exchange',
            calibration_auto=False,
            calibration_max_rounds=1,
            codex_command=[sys.executable, str(ROOT / 'tests' / 'fake_codex.py')],
            smallshot_size={'portrait': [60, 80], 'landscape': [80, 60]},
            final_size={'portrait': [60, 80], 'landscape': [80, 60]},
            calibration_timeout_sec=10,
        )
        self.studio = Studio(self.root, config)
        self.addCleanup(self.studio.calibration.close)
        self.product = self.root / '可编辑材质产品'
        self.product.mkdir()
        (self.product / '模型.max').write_text('开发占位', encoding='utf-8')
        self.task = self.studio.create({'product_dir': str(self.product)})
        self.task_id = self.task['id']
        self.folder = self.studio.task_dir(self.task_id)

    def _finish_calibration(self, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run_once(self.studio.exchange, ROOT / 'tests' / 'fixtures' / 'L型咖啡柜_绑定.fbx')
            task = self.studio.merge(self.task_id)['task']
            if task['ai_calibration']['state'] in ('waiting_user', 'failed') and not self.studio.calibration.active:
                return task
            time.sleep(0.04)
        self.fail('校准未在超时时间内完成。')

    def test_scene_materials_detail_fallback_and_update_actions_with_clamping(self):
        # 尚未开展校准时，读取 preview/scene_manifest.json
        preview_manifest = {
            'materials': [
                {'name': '板材01', 'kind': 'wood', 'class': 'CoronaPhysicalMtl', 'objects': 4,
                 'color': [160, 120, 80], 'bitmap': 'oak.jpg', 'params': {'roughness': 0.45}},
                {'name': '门板清玻', 'kind': 'glass', 'class': 'CoronaPhysicalMtl', 'objects': 2,
                 'color': [240, 245, 245], 'bitmap': None, 'params': {'ior': 1.52}},
                {'name': '柜门胡桃', 'kind': 'wood', 'class': 'CoronaLegacyMtl', 'objects': 3,
                 'color': [95, 65, 45], 'bitmap': 'walnut.png', 'params': {}},
            ],
            'environment': {'reflect_map': 'studio_env.hdr'},
        }
        atomic_json(self.folder / 'preview' / 'scene_manifest.json', preview_manifest)

        detail0 = scene_materials_detail(self.studio, self.studio.load(self.task_id))
        self.assertEqual(detail0['environment'], {'reflect_map': 'studio_env.hdr'})
        self.assertIsNone(detail0['effective_overrides'])
        by_name = {m['name']: m for m in detail0['materials']}
        self.assertIsNone(by_name['板材01']['auto_ref'])
        self.assertIsNone(by_name['门板清玻']['auto_ref'])
        self.assertEqual(by_name['柜门胡桃']['auto_ref'], '胡桃木')
        self.assertEqual(by_name['柜门胡桃']['effective_ref'], '胡桃木')

        # 1) action='set'：数值范围截断、非玻璃剔除玻璃专属字段、绑定保存
        res = update_task_materials(
            self.studio,
            self.task_id,
            {
                'action': 'set',
                'material_overrides': {
                    'materials': {
                        '板材01': {
                            'hue_shift_deg': -99,
                            'saturation_mul': 5.0,
                            'brightness_mul': 9.0,
                            'contrast_mul': 0.1,
                            'reflection_mul': -3.0,
                            'glossiness_delta': 2.0,
                            'ior': 1.6,            # 木材应忽略玻璃专属字段
                            'opacity_mul': 0.6,
                        },
                        '门板清玻': {
                            'refraction_mul': 9.0,
                            'ior': 1.1,
                            'opacity_mul': 0.1,
                        },
                        '柜门胡桃': {               # 全部中性值应自动精简移除
                            'hue_shift_deg': 0,
                            'saturation_mul': 1.0,
                            'brightness_mul': 1.0,
                        },
                    },
                    'environment': {'reflection_strength_mul': 0.0},
                },
                'material_bindings': {
                    '板材01': '摩卡红橡',
                    '柜门胡桃': '__none__',
                },
            },
        )
        saved_overrides = res['task']['material_overrides']
        self.assertEqual(
            saved_overrides['materials']['板材01'],
            {
                'hue_shift_deg': -15,
                'saturation_mul': 1.4,
                'brightness_mul': 2.5,
                'contrast_mul': 0.7,
                'reflection_mul': 0.0,
                'glossiness_delta': 0.3,
            },
        )
        self.assertEqual(
            saved_overrides['materials']['门板清玻'],
            {'refraction_mul': 1.5, 'ior': 1.3, 'opacity_mul': 0.5},
        )
        self.assertNotIn('柜门胡桃', saved_overrides['materials'])
        self.assertEqual(saved_overrides['environment'], {'reflection_strength_mul': 0.0})
        detail1 = res['materials_detail']
        by_name1 = {m['name']: m for m in detail1['materials']}
        self.assertEqual(by_name1['板材01']['bound_ref'], '摩卡红橡')
        self.assertEqual(by_name1['板材01']['effective_ref'], '摩卡红橡')
        self.assertEqual(by_name1['柜门胡桃']['bound_ref'], '__none__')
        self.assertIsNone(by_name1['柜门胡桃']['effective_ref'])

        # 非法分组名返回 400
        with self.assertRaises(StudioError) as bad_group:
            update_task_materials(self.studio, self.task_id, {'action': 'set', 'material_bindings': {'板材01': '不存在材质'}})
        self.assertEqual(bad_group.exception.status, 400)

        # 2) action='reset_material'：仅重置单个材质，保留其他材质与环境
        res_reset_one = update_task_materials(
            self.studio, self.task_id, {'action': 'reset_material', 'name': '板材01'}
        )
        self.assertNotIn('板材01', res_reset_one['task']['material_overrides']['materials'])
        self.assertIn('门板清玻', res_reset_one['task']['material_overrides']['materials'])
        self.assertEqual(res_reset_one['task']['material_overrides']['environment'], {'reflection_strength_mul': 0.0})

        # 3) action='reset_all'：清空全部手动覆盖，但保留 material_bindings
        res_reset_all = update_task_materials(self.studio, self.task_id, {'action': 'reset_all'})
        self.assertIsNone(res_reset_all['task']['material_overrides'])
        self.assertIsNone(res_reset_all['materials_detail']['effective_overrides'])
        self.assertEqual(res_reset_all['task']['material_bindings'], {'板材01': '摩卡红橡', '柜门胡桃': '__none__'})

        # 4) AI 校准进行中（rendering / thinking）时拒绝修改，返回 409
        for active_state in ('rendering', 'thinking'):
            task = self.studio.load(self.task_id)
            task['ai_calibration']['state'] = active_state
            self.studio.save(task)
            with self.assertRaises(StudioError) as conflict:
                update_task_materials(self.studio, self.task_id, {'action': 'reset_all'})
            self.assertEqual(conflict.exception.status, 409)
            self.assertEqual(str(conflict.exception), 'AI 校准进行中，请等本轮结束后再修改材质参数。')
        task = self.studio.load(self.task_id)
        task['ai_calibration']['state'] = 'idle'
        self.studio.save(task)

    def test_effective_overrides_merge_lighting_reset_and_calibration_roundtrip(self):
        # 1) 先设置手动材质参数，启动校准时第 0 轮应继承 effective_overrides(task)
        update_task_materials(
            self.studio,
            self.task_id,
            {
                'action': 'set',
                'material_overrides': {
                    'materials': {'摩卡红橡': {'brightness_mul': 1.35}},
                    'environment': {'reflection_strength_mul': 0.4},
                },
            },
        )
        started = self.studio.calibration.action(self.task_id, {'action': 'start', 'user_note': '测试继承手动材质'})
        self.assertEqual(
            started['ai_calibration']['rounds'][0]['overrides'],
            {'materials': {'摩卡红橡': {'brightness_mul': 1.35}}, 'environment': {'reflection_strength_mul': 0.4}},
        )

        finished = self._finish_calibration()
        self.assertEqual(finished['ai_calibration']['state'], 'waiting_user')
        self.assertEqual(len(finished['ai_calibration']['rounds']), 2)

        # 2) accept（「用这一轮」）写入 accepted_overrides 并清空已包含的 material_overrides，避免重复叠加
        accepted_task = self.studio.calibration.action(self.task_id, {'action': 'accept', 'round': 1})
        self.assertIsNone(accepted_task.get('material_overrides'))
        self.assertIsNotNone(accepted_task['ai_calibration']['accepted_overrides'])
        base_accepted = accepted_task['ai_calibration']['accepted_overrides']
        self.assertEqual(effective_overrides(accepted_task), base_accepted)

        # 3) 在已接受 AI 轮次之上叠加用户手动覆盖（按字段覆盖，保留 AI 灯光调整）
        layered = update_task_materials(
            self.studio,
            self.task_id,
            {
                'action': 'set',
                'material_overrides': {
                    'materials': {'摩卡红橡': {'brightness_mul': 1.8, 'hue_shift_deg': 4}},
                    'environment': {'reflection_strength_mul': 0.0},
                },
            },
        )
        eff = layered['materials_detail']['effective_overrides']
        self.assertEqual(eff['materials']['摩卡红橡']['brightness_mul'], 1.8)
        self.assertEqual(eff['materials']['摩卡红橡']['hue_shift_deg'], 4)
        self.assertEqual(eff['environment']['reflection_strength_mul'], 0.0)
        self.assertEqual(eff.get('lights'), base_accepted.get('lights'))

        # 4) retry（「再调一轮」）继承合并后的 effective_overrides 作为新一轮起始值
        retried = self.studio.calibration.action(self.task_id, {'action': 'retry', 'user_note': '继续微调'})
        self.assertEqual(retried['ai_calibration']['rounds'][-1]['overrides'], eff)
        self._finish_calibration()

        # 5) action='from_round'：从第 1 轮复制材质与环境覆盖供手动继续调
        copied = update_task_materials(self.studio, self.task_id, {'action': 'from_round', 'round': 1})
        round1_mat = copied['task']['ai_calibration']['rounds'][1]['overrides']['materials']
        self.assertEqual(copied['task']['material_overrides']['materials']['摩卡红橡']['brightness_mul'],
                         round1_mat['摩卡红橡']['brightness_mul'])

        # 6) undo（「撤销 AI 调整」）只清空 accepted_overrides 与 accepted_round，保留手动 material_overrides
        undone = self.studio.calibration.action(self.task_id, {'action': 'undo'})
        self.assertIsNone(undone['ai_calibration']['accepted_overrides'])
        self.assertIsNone(undone['ai_calibration']['accepted_round'])
        self.assertIsNotNone(undone['material_overrides'])

        # 7) 再次 accept 后手动修改材质，再改变任务打光：accepted_overrides 重置，但 material_overrides 完整保留！
        self.studio.calibration.action(self.task_id, {'action': 'accept', 'round': 1})
        update_task_materials(
            self.studio,
            self.task_id,
            {
                'action': 'set',
                'material_overrides': {'materials': {'摩卡红橡': {'saturation_mul': 1.2}}, 'environment': {}},
            },
        )
        relit = self.studio.update(self.task_id, {'lighting': {'enabled': True, 'azimuth_deg': -45}})
        self.assertIsNone(relit['ai_calibration']['accepted_overrides'])
        self.assertIsNone(relit['ai_calibration']['accepted_round'])
        self.assertEqual(relit['material_overrides'], {'materials': {'摩卡红橡': {'saturation_mul': 1.2}}, 'environment': {}})
        self.assertEqual(
            effective_overrides(relit),
            {'materials': {'摩卡红橡': {'saturation_mul': 1.2}}, 'environment': {}},
        )


if __name__ == '__main__':
    unittest.main()
