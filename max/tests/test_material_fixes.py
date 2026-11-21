"""使用只带属性和值的材质，验证中文版 Corona 的实测修正。"""
import math
from pathlib import Path
import sys
import types
import unittest
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wb_core as core
import test_wb_core as existing


class Material:
    def __init__(self, **properties):
        self.__dict__.update(properties)


class MaterialPropertyTests(unittest.TestCase):
    def test_families_need_no_class_name(self):
        for material, expected in (
            (Material(colorDiffuse=(149,126,97), levelReflect=.8), 'legacy'),
            (Material(baseColor=(1,2,3), basetexmap=None, baseroughness=.7), 'physical'),
            (Material(directMtlOn=False, directMtl=None), 'ray_switch'),
            (Material(materialList=[]), 'multi'), (Material(), None)):
            with self.subTest(expected=expected):
                self.assertEqual(core.material_family(material), expected)

    def test_measured_property_names(self):
        legacy = Material(colorDiffuse=0, texmapDiffuse=None, levelReflect=.5, reflectGlossiness=.78,
                          IOR=1.5, levelOpacity=1, levelRefract=.5)
        physical = Material(baseColor=0, basetexmap=None, baseIor=1.507, baseroughness=.7,
                            refractionamount=.8, opacityLevel=1)
        for material, expected in (
            (legacy, ('colorDiffuse','texmapDiffuse','levelReflect','reflectGlossiness','levelRefract','IOR','levelOpacity')),
            (physical, ('baseColor','basetexmap','baseIor','baseroughness','refractionamount','baseIor','opacityLevel'))):
            self.assertEqual(tuple(core.material_property(material, key) for key in
                ('color','map','reflection_mul','glossiness_delta','refraction_mul','ior','opacity_mul')), expected)
        self.assertIn('baseIor', core.material_parameters(physical))

    def test_physical_f0_roundtrip_limits_and_no_diffuse_substitution(self):
        for n, multiplier in ((1.507,1), (1.507,.5), (1.507,2), (1.01,2)):
            adjusted = core.physical_reflection_ior(n, multiplier)
            self.assertAlmostEqual(((adjusted-1)/(adjusted+1))**2, ((n-1)/(n+1))**2*multiplier)
        self.assertEqual(core.physical_reflection_ior(1.507,0), 1.01)
        high = core.physical_reflection_ior(10,2)
        self.assertAlmostEqual(((high-1)/(high+1))**2,.2)
        self.assertEqual(core.material_property(Material(baseLevel=1, baseColor=0), 'reflection_mul'), None)
        self.assertAlmostEqual(core.adjusted_scalar(1.507,'reflection_mul',2,'baseIor'), core.physical_reflection_ior(1.507,2))

    def test_ray_direct_first_fallback_and_identity_deduplication(self):
        direct, gi = Material(baseIor=1.5), Material(colorDiffuse=0)
        ray = Material(giMtlOn=True, giMtl=gi, reflectMtl=gi, directMtlOn=False, directMtl=direct,
                       refractMtl=None)
        self.assertEqual(core.ray_switch_children(ray), [direct, gi])
        ray.directMtl = None
        self.assertEqual(core.ray_switch_children(ray), [gi])

    def test_dominant_material_ids_sparse_ties_empty_and_missing(self):
        a, b = Material(), Material()
        self.assertIs(core.dominant_submaterial([a,b], [8,3], [8,3,3]), b)
        self.assertIs(core.dominant_submaterial([a,b], [8,3], [8,3]), b)
        self.assertIsNone(core.dominant_submaterial([None,b], [8,3], [8,8,3]))
        self.assertIsNone(core.dominant_submaterial([a,b], [8,3], []))
        self.assertIsNone(core.dominant_submaterial([a,b], [8,3], [9,9]))


class MeasuredRunnerTests(unittest.TestCase):
    setUp = existing.RunnerContractTests.setUp

    def prepare_runtime(self):
        self.runtime.classOf.side_effect = lambda obj: 'Corona光线切换' if hasattr(obj, 'directMtlOn') else ('_Corona物理材质' if hasattr(obj, 'baseIor') else 'Corona传统材质')
        self.runtime.getHandleByAnim.side_effect = id
        self.runtime.getPropNames.side_effect = lambda obj: list(vars(obj))
        self.runtime.getNumSubTexmaps.return_value = 0
        self.runtime.Color.side_effect = lambda r,g,b: Material(r=r,g=g,b=b)
        self.runtime.renderers.current = Material(bg_texmapReflect=None)
        self.runner._calibration_unsupported = []

    def test_chinese_legacy_manifest_and_actual_glass_properties(self):
        self.prepare_runtime()
        material = Material(name='奶油白', colorDiffuse=Material(r=149,g=126,b=97), texmapDiffuse=None,
                            levelDiffuse=1, levelReflect=.4, reflectGlossiness=.78, fresnelIor=1.6,
                            IOR=1.5, levelRefract=0, levelOpacity=1)
        data = self.runner._material_data(material)
        self.assertEqual((data['kind'], data['color']), ('solid', [149,126,97]))
        self.assertEqual(data['params']['reflectGlossiness'],.78)
        self.assertEqual(data['params']['fresnelIor'],1.6)
        self.runner._apply_material(material, {'reflection_mul':2, 'glossiness_delta':.2})
        self.assertEqual(material.levelReflect,.8)
        self.assertAlmostEqual(material.reflectGlossiness,.98)
        material.name = '长虹玻璃'
        self.runner._apply_material(material, {'ior':1.8, 'opacity_mul':.5, 'refraction_mul':1.5})
        self.assertEqual((material.IOR, material.levelOpacity), (1.8,.5))
        self.assertEqual(material.fresnelIor,1.6)

    def test_physical_roughness_modes_and_f0_keep_base_level(self):
        self.prepare_runtime()
        for mode, expected in ((1,.5), (0,.9)):
            mat = Material(name='木', baseIor=1.507, baseroughness=.7, roughnessMode=mode, baseLevel=.8)
            self.runner._apply_material(mat, {'reflection_mul':2, 'glossiness_delta':.2})
            self.assertAlmostEqual(mat.baseroughness,expected)
            self.assertAlmostEqual(mat.baseIor,core.physical_reflection_ior(1.507,2))
            self.assertEqual(mat.baseLevel,.8)

    def test_ray_all_unique_children_and_outer_name_in_manifest_and_legend(self):
        self.prepare_runtime()
        texture = Material(filename='D:/木纹/oak.jpg')
        direct = Material(name='内部物理', baseColor=Material(r=120,g=80,b=60), basetexmap=texture,
                          baseIor=1.507, baseroughness=.7, roughnessMode=1)
        gi = Material(name='内部传统', colorDiffuse=Material(r=100,g=100,b=100), levelReflect=.3)
        ray = Material(name='摩卡红橡', giMtlOn=True, giMtl=gi, directMtlOn=True, directMtl=direct,
                       reflectMtlOn=False, reflectMtl=direct, refractMtlOn=False, refractMtl=gi)
        nodes = [Material(name='顶板', material=ray), Material(name='侧板',material=ray)]
        self.runner.exchange = existing.TMP_DIR/('materials-'+uuid4().hex[:8])
        self.runner.job.update(want_manifest=True, overrides={'materials': {'摩卡红橡': {'reflection_mul':2}}})
        self.runner._bbox = mock.Mock(return_value=((0,0,0),(2,4,6)))
        self.runner._apply_calibration(nodes, [], (0,0,0), 1)
        self.assertAlmostEqual(direct.baseIor,core.physical_reflection_ior(1.507,2))
        self.assertEqual(gi.levelReflect,.6)
        manifest = core.read_json(self.runner._calibration_dir()/'scene_manifest.json')
        self.assertEqual(len(manifest['materials']),1)
        entry = manifest['materials'][0]
        self.assertEqual((entry['name'],entry['kind'],entry['bitmap'],entry['objects']),('摩卡红橡','wood','oak.jpg',2))
        self.assertEqual(entry['color'],[120,80,60])
        self.assertEqual(entry['params']['baseroughness'],.7)
        self.assertAlmostEqual(entry['params']['baseIor'],direct.baseIor)
        self.assertEqual(manifest['unsupported'],[])
        self.assertEqual(list(core.read_json(self.runner._calibration_dir()/'regions/legend.json').values()),['摩卡红橡'])

    def test_ray_direct_kind_controls_glass_adjustment_for_all_children(self):
        self.prepare_runtime()
        direct = Material(name='长虹玻璃', baseIor=1.507, refractionamount=.8, opacityLevel=1)
        gi = Material(name='内部', IOR=1.5, levelRefract=.8, levelOpacity=1, colorDiffuse=Material(r=1,g=2,b=3))
        ray = Material(name='外层', giMtlOn=True, giMtl=gi, directMtlOn=True, directMtl=direct)
        self.assertEqual(self.runner._material_data(ray)['kind'],'glass')
        self.runner._apply_material(ray, {'ior':1.8, 'opacity_mul':.5, 'refraction_mul':.5})
        self.assertEqual((direct.baseIor,direct.opacityLevel,direct.refractionamount),(1.8,.5,.4))
        self.assertEqual((gi.IOR,gi.levelOpacity,gi.levelRefract),(1.8,.5,.4))

    def test_nested_ray_cycle_does_not_loop_and_direct_fallback(self):
        self.prepare_runtime()
        leaf = Material(name='奶油白', colorDiffuse=Material(r=149,g=126,b=97), levelReflect=.4)
        ray = Material(name='外层', directMtlOn=False, directMtl=None, giMtl=leaf)
        nested = Material(name='嵌套', directMtlOn=True, directMtl=ray, giMtl=leaf)
        ray.reflectMtl = nested
        data = self.runner._material_data(ray)
        self.assertEqual(data['color'],[149,126,97])
        self.runner._apply_material(ray, {'reflection_mul':2})
        self.assertEqual(leaf.levelReflect,.8)

    def test_multi_face_read_failure_does_not_fall_back_to_first_slot(self):
        self.prepare_runtime()
        multi = Material(name='多维', materialList=[Material(name='木')])
        node = Material(name='板', material=multi)
        self.runtime.snapshotAsMesh.side_effect = RuntimeError('读取失败')
        self.assertIsNone(self.runner._multi_main(node,multi))
        self.assertTrue(any('未分配材质' in s for s in self.runner._calibration_unsupported))


if __name__ == '__main__':
    unittest.main()
