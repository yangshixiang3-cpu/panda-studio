"""打光描述、数据、校准保护和入库；只使用临时产品与假执行器。"""
import copy
import io
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from server.core import Studio,StudioError,read_json,atomic_json
from server.lighting import validate_lighting,merged_lighting,lighting_text,protect_light_overrides,MAX_LIGHTING_BYTES
from server.calibration import clamp_overrides,analyze_round,select_images
from server.app import LocalServer,handler_for
from tools.fake_runner import run_once
from test_temp_dirs import test_directory
from fixture_files import needs_product_fbx


def png():
    out=io.BytesIO();Image.new('RGB',(1200,900),'white').save(out,'PNG');return out.getvalue()


class LightingDescriptionTests(unittest.TestCase):
    def test_shared_cases(self):
        cases=read_json(ROOT/'tests/fixtures/lighting-cases.json')
        for case in cases:
            with self.subTest(value=case['lighting']):self.assertEqual(lighting_text(dict(case['lighting'], enabled=True)),case['text'])

    def test_spec_example(self):
        self.assertEqual(lighting_text({'enabled':True,'azimuth_deg':-45}), '主光从左前方斜上方约 35° 照来（水平 −45°，高 35°）；左侧受光较亮、右侧偏暗；明暗对比中等。')

    def test_direction_height_contrast_boundaries(self):
        for az,word in [(-19,'正前方'),(-20,'左前方'),(-69,'左前方'),(-70,'左侧'),(-110,'左侧'),(-111,'左后方（逆光）'),(110,'右侧'),(111,'右后方（逆光）')]:
            self.assertIn(word,lighting_text({'enabled':True,'azimuth_deg':az}))
        for el,word in [(19,'低角度'),(20,'斜上方'),(55,'斜上方'),(56,'顶面最亮')]:self.assertIn(word,lighting_text({'enabled':True,'azimuth_deg':0,'elevation_deg':el}))
        for c,word in [(.299,'柔和'),(.3,'中等'),(.7,'中等'),(.701,'强烈')]:self.assertIn('明暗对比'+word,lighting_text({'enabled':True,'azimuth_deg':0,'contrast':c}))

    def test_validation_invalid_and_clamped(self):
        for value in [None,False,'45',float('nan'),float('inf')]:self.assertIsNone(validate_lighting({'azimuth_deg':value})['azimuth_deg'])
        self.assertEqual(validate_lighting({'azimuth_deg':999,'elevation_deg':-1,'intensity':9,'contrast':-2}),dict(enabled=False,azimuth_deg=180,elevation_deg=5,intensity=1.5,contrast=0))
        self.assertEqual(validate_lighting({'azimuth_deg':-999,'elevation_deg':999,'intensity':0,'contrast':9}),dict(enabled=False,azimuth_deg=-180,elevation_deg=85,intensity=.6,contrast=1))

    def test_merge_whole_set_and_explicit_template(self):
        task={'lighting':dict(enabled=True,azimuth_deg=-45,elevation_deg=60,intensity=1.5,contrast=1)}
        self.assertEqual(merged_lighting(task,{'lighting':None}),task['lighting'])
        self.assertEqual(merged_lighting(task,{'lighting':{'azimuth_deg':45}}),dict(enabled=False,azimuth_deg=45,elevation_deg=35,intensity=1,contrast=.5))
        self.assertIsNone(merged_lighting(task,{'lighting':{'azimuth_deg':None}})['azimuth_deg'])

    def test_calibration_truncation_and_no_mutation(self):
        manifest={'materials':[{'name':'木','kind':'wood'}],'lights':[{'name':'主灯'}]}
        value={'materials':{'木':{'brightness_mul':2}},'lights':{'intensity_mul':2,'rotate_deg':45,'per_light':{'主灯':{'intensity_mul':2}}},'items':{'1':{'lights':{'rotate_deg':-45}}}}
        original=copy.deepcopy(value)
        for intensity,bound in [(2,1.15),(.1,.85)]:
            value['lights']['intensity_mul']=intensity
            result=clamp_overrides(value,manifest,[{'no':1,'view':'main','lighting':{'enabled':True,'azimuth_deg':-45}}])
            self.assertEqual(result['lights'],dict(intensity_mul=bound,rotate_deg=0,per_light={}))
            self.assertEqual(result['items']['1']['lights']['rotate_deg'],0)
            self.assertEqual(result['materials']['木']['brightness_mul'],2)
        value['lights']['intensity_mul']=2;self.assertEqual(value,original)
        self.assertEqual(clamp_overrides(value,manifest,[{'no':1,'view':'main'}])['lights']['rotate_deg'],45)
        for light, item_enabled in [(dict(azimuth_deg=-45), True),
                                    (dict(enabled=False, azimuth_deg=-45), True),
                                    (dict(enabled=True, azimuth_deg=None), True),
                                    (dict(enabled=True, azimuth_deg=-45), False)]:
            with self.subTest(light=light, item_enabled=item_enabled):
                task = {'lighting':light, 'items':[{'enabled':item_enabled}]}
                self.assertEqual(protect_light_overrides(value, task), original)


@needs_product_fbx
class LightingFlowTests(unittest.TestCase):
    def setUp(self):
        self.root=test_directory(self, 'light-')
        config=read_json(ROOT/'config.json');config.update(exchange_dir='exchange',codex_command=[sys.executable,str(ROOT/'tests/fake_codex.py')],smallshot_size={'portrait':[120,160],'landscape':[160,120]},final_size={'portrait':[120,160],'landscape':[160,120]})
        self.studio=Studio(self.root,config);self.addCleanup(self.studio.calibration.close)
        self.product=self.root/'临时产品';self.product.mkdir();(self.product/'柜体.max').write_bytes(b'fake')
        task=self.studio.create({'product_dir':str(self.product)})
        self.id=task['id']
        main = task['items'][0]
        items = [main]
        for no, (label, view) in enumerate((('正面', 'front'), ('侧面', 'side')), 2):
            items.append({**main, 'no':no, 'label':label, 'view':view})
        self.task=self.studio.update(self.id, {'items':items})

    def light(self):
        return self.studio.update(self.id,{'lighting':{'enabled':True,'azimuth_deg':-45}})

    def finish(self):
        self.studio.submit(self.id,{'mode':'final'});run_once(self.studio.exchange,ROOT/'tests/fixtures/L型咖啡柜_绑定.fbx')

    def test_roundtrip_merge_render_and_reset_state(self):
        task=self.light();items=task['items'];items[1]['lighting']={'enabled':True,'azimuth_deg':45,'intensity':1.5};items[2]['lighting']={'azimuth_deg':None}
        self.studio.update(self.id,{'items':items})
        job=self.studio.submit(self.id,{'mode':'final'})
        self.assertEqual(job['items'][0]['lighting']['azimuth_deg'],-45);self.assertEqual(job['items'][1]['lighting']['azimuth_deg'],45)
        self.assertIsNone(job['items'][2]['lighting']['azimuth_deg']);self.assertEqual(job['items'][2]['lighting_text'],'模板默认灯光')
        self.assertTrue(all('lighting_text' in i for i in job['items']))
        self.assertEqual(self.studio.load(self.id)['items'][0]['lighting_text'],job['items'][0]['lighting_text'])
        self.studio.update(self.id,{'lighting':{'enabled':True,'azimuth_deg':90}})
        task=self.studio.load(self.id);self.assertEqual(task['items'][0]['final']['state'],'none');self.assertEqual(task['items'][1]['final']['state'],'queued')

    def test_legacy_direction_stays_saved_while_rendering_with_template(self):
        raw = read_json(self.studio.task_dir(self.id)/'task.json')
        self.assertIs(raw['lighting']['enabled'], False)
        raw['lighting'] = dict(azimuth_deg=-70, elevation_deg=52, intensity=1.2, contrast=.8)
        raw['items'][0]['lighting'] = dict(azimuth_deg=80)
        raw['ai_calibration'].update(accepted_overrides={'lights':{'rotate_deg':30}}, accepted_round=0)
        atomic_json(self.studio.task_dir(self.id)/'task.json', raw)
        task = self.studio.load(self.id)
        self.assertIs(task['lighting']['enabled'], False)
        self.assertIs(task['items'][0]['lighting']['enabled'], False)
        self.assertEqual(task['items'][0]['lighting']['azimuth_deg'], 80)
        light = task['lighting']
        template_job = self.studio.submit(self.id, {'mode':'smallshot'})
        self.assertTrue(all(item['lighting']['azimuth_deg'] is None for item in template_job['items']))
        self.assertEqual(template_job['overrides']['lights']['rotate_deg'], 30)
        latent = self.studio.update(self.id, {'lighting':dict(light, azimuth_deg=-72)})
        self.assertEqual(latent['ai_calibration']['accepted_round'], 0)
        self.assertEqual(latent['items'][1]['smallshot']['job_id'], template_job['job_id'])
        light = latent['lighting']
        for enabled in (True, False, True):
            task = self.studio.update(self.id, {'lighting':dict(light, enabled=enabled)})
            self.assertEqual(task['lighting'], dict(light, enabled=enabled))
            self.assertIsNone(task['ai_calibration']['accepted_round'])
            job = self.studio.submit(self.id, {'mode':'final'})
            self.assertIsNone(job['items'][0]['lighting']['azimuth_deg'])
            self.assertEqual(job['items'][1]['lighting']['azimuth_deg'], -72 if enabled else None)
            run_once(self.studio.exchange, ROOT/'tests/fixtures/L型咖啡柜_绑定.fbx')

    def test_each_submission_reads_raw_tuning_from_disk_and_updates_task_snapshot(self):
        path=self.root/'config.json'
        first=dict(fill_min=.13, probe=True, future_field={'原样': [1, None]})
        second=dict(key_size_min=.12, key_size_max=.4, bg_fill_keep=.8)
        for index, (mode, tuning) in enumerate((('smallshot', first), ('final', second)), 1):
            with self.subTest(mode=mode):
                atomic_json(path,dict(self.studio.config, lighting_tuning=tuning, key_light_intensity=11+index))
                job=self.studio.submit(self.id,{'mode':mode})
                saved=read_json(self.studio.exchange/'jobs'/f"{job['job_id']}.json")
                self.assertEqual(saved['lighting_tuning'],tuning)
                self.assertEqual(saved['key_light_intensity'],11+index)
                current=self.studio.describe(self.id)['task']
                self.assertEqual(current['lighting_tuning'],tuning)
                self.assertEqual(current['key_light_intensity'],11+index)
            if index == 1:
                first_job_path=self.studio.exchange/'jobs'/f"{job['job_id']}.json"
        self.assertEqual(read_json(first_job_path)['lighting_tuning'],first)

    def test_absent_config_section_is_an_empty_snapshot(self):
        config=dict(self.studio.config)
        config.pop('lighting_tuning',None)
        atomic_json(self.root/'config.json',config)
        job=self.studio.submit(self.id,{'mode':'smallshot'})
        self.assertEqual(job['lighting_tuning'],{})
        self.assertEqual(job['key_light_intensity'],config['key_light_intensity'])

    def test_png_content_validation_backups_and_path_rejection(self):
        self.light()
        for no,content in [(1,b'jpg'),(1,b'\x89PNG\r\n\x1a\nBAD'),('../1',png()),(99,png()),(1,b'x'*(MAX_LIGHTING_BYTES+1))]:
            with self.subTest(no=no),self.assertRaises(StudioError):self.studio.add_lighting_image(self.id,no,content)
        result=self.studio.add_lighting_image(self.id,1,png());self.studio.add_lighting_image(self.id,1,png())
        self.assertTrue(Path(result['file']+'.prev').is_file())

    def test_http_upload_png_size_type_origin(self):
        self.light()
        server=LocalServer(('127.0.0.1',0),handler_for(self.studio));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url=f'http://127.0.0.1:{server.server_port}/api/tasks/{self.id}/lighting-images?no=1'
            request=urllib.request.Request(url,data=png(),headers={'Content-Type':'image/png'})
            with urllib.request.urlopen(request) as response:self.assertEqual(response.status,201)
            for headers,status in [({'Content-Type':'image/jpeg'},415),({'Content-Type':'image/png','Origin':'http://evil.test'},403),({'Content-Type':'image/png','Content-Length':str(MAX_LIGHTING_BYTES+1)},413)]:
                with self.subTest(headers=headers),self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(urllib.request.Request(url,data=b'x',headers=headers))
                self.assertEqual(caught.exception.code,status)
        finally:server.shutdown();server.server_close();thread.join()

    def test_delivery_and_old_light_archive(self):
        self.light();self.finish()
        for item in self.task['items']:self.studio.add_lighting_image(self.id,item['no'],png())
        old=self.product/'光向参考/1.png';old.parent.mkdir();old.write_bytes('旧光向'.encode('utf-8'))
        self.studio.deliver(self.id,self.studio.delivery_plan(self.id)['token'])
        self.assertEqual(old.read_bytes(),png());archives=list((old.parent/'旧版').glob('*/1.png'));self.assertEqual(len(archives),1);self.assertEqual(archives[0].read_bytes(),'旧光向'.encode('utf-8'))
        records=read_json(self.product/'sku/白底清单.json');self.assertEqual(records[0]['lighting']['azimuth_deg'],-45);self.assertIn('左前方',records[0]['lighting_text'])

    def test_unlit_delivery_ignores_existing_diagram(self):
        task = self.light()
        self.studio.add_lighting_image(self.id,1,png())
        items = task['items']
        items[1]['lighting'] = dict(enabled=True, azimuth_deg=45)
        self.studio.update(self.id, {'lighting':dict(task['lighting'], enabled=False), 'items':items})
        with self.assertRaises(StudioError):self.studio.add_lighting_image(self.id,1,png())
        self.finish()
        with self.assertRaisesRegex(StudioError, '缺少光向示意图'):self.studio.delivery_plan(self.id)
        self.studio.add_lighting_image(self.id,2,png())
        self.studio.deliver(self.id,self.studio.delivery_plan(self.id)['token'])
        self.assertEqual([p.name for p in (self.product/'光向参考').iterdir()], ['2.png'])
        records = read_json(self.product/'sku/白底清单.json')
        self.assertEqual(records[0]['lighting_text'], '模板默认灯光')
        self.assertIs(records[0]['lighting']['enabled'], False)
        self.assertEqual(records[0]['lighting']['azimuth_deg'], -45)
        self.assertIs(records[1]['lighting']['enabled'], True)

    def test_fake_codex_prompt_image_and_truncation(self):
        task=self.light()
        for i in task['items']:self.studio.add_lighting_image(self.id,i['no'],png())
        job=self.studio._submit_render(task,'smallshot',round_no=0)
        run_once(self.studio.exchange,ROOT/'tests/fixtures/L型咖啡柜_绑定.fbx')
        result=analyze_round(self.studio,task,{'n':0,'overrides':None},job)
        folder=self.studio.task_dir(self.id)/'calibration/round-0'
        prompt=(folder/'prompt.txt').read_text(encoding='utf-8');self.assertIn('光向和明暗关系以使用者设定为准，不改光向',prompt);self.assertIn('左前方',prompt)
        self.assertEqual(result['overrides']['lights']['rotate_deg'],0);self.assertEqual(result['overrides']['lights']['per_light'],{})
        inputs=read_json(folder/'input.json');self.assertTrue(any('使用者设定的光向' in i['role'] for i in inputs['images']))
        command=read_json(folder/'command.json');self.assertIn(str(ROOT/'tests/fake_codex.py'),command)
