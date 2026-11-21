"""校准和参考库隔离测试；Codex 使用本地替身。"""
import copy
import io
import json
import math
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.calibration import clamp_overrides, command_for, validate_schema, select_images, overrides_from_arrays
from server.color_stats import color_statistics, srgb_to_lab
from server.core import Studio, StudioError, atomic_json, read_json
from server.ref_library import RefLibrary, build_library, material_key
from server.app import LocalServer, handler_for
from tools.fake_runner import run_once
from test_temp_dirs import test_directory
from fixture_files import needs_product_fbx


class OverrideTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {'materials': [{'name': '木', 'kind': 'wood'}, {'name': '玻璃', 'kind': 'glass'}], 'lights': [{'name': '主灯'}]}
        self.items = [{'no': 1, 'view': 'main'}, {'no': 2, 'view': 'front'}]

    def test_clamp_every_material_limit(self):
        values = {'hue_shift_deg': 999, 'saturation_mul': -2, 'brightness_mul': 9, 'contrast_mul': 0,
                  'reflection_mul': 8, 'glossiness_delta': -1, 'ior': 1.1, 'refraction_mul': 9, 'opacity_mul': 0}
        original = copy.deepcopy(values)
        result = clamp_overrides({'materials': {'玻璃': values}}, self.manifest, self.items)['materials']['玻璃']
        # 2026-11-20 亮度上限从 1.4 放宽到 2.5（深色木纹实测提亮不明显）
        self.assertEqual(result, {'hue_shift_deg': 15, 'saturation_mul': .6, 'brightness_mul': 2.5,
                                 'contrast_mul': .7, 'reflection_mul': 2, 'glossiness_delta': -.3,
                                 'ior': 1.3, 'refraction_mul': 1.5, 'opacity_mul': .5})
        self.assertEqual(values, original)

    def test_clamp_lights_environment_main_only_and_unknowns(self):
        result = clamp_overrides({'materials': {'未知': {}, '木': {'ior': 1.5, 'opacity_mul': .7, 'render_quality': 8}},
                                  'lights': {'intensity_mul': 9, 'rotate_deg': -400, 'per_light': {'主灯': {'intensity_mul': 9}, '未知灯': {'intensity_mul': 1}}},
                                  'environment': {'reflection_strength_mul': 0, 'exposure': 5},
                                  'items': {'1': {'lights': {'rotate_deg': 200}}, '2': {'lights': {'rotate_deg': 50}}},
                                  'render_preset': '禁止'}, self.manifest, self.items)
        self.assertEqual(result['materials'], {'木': {}})
        self.assertEqual(result['lights'], {'intensity_mul': 1.6, 'rotate_deg': -90, 'per_light': {'主灯': {'intensity_mul': 2}}})
        self.assertEqual(result['environment'], {'reflection_strength_mul': .5})
        self.assertEqual(result['items'], {'1': {'lights': {'rotate_deg': 90}}})
        self.assertNotIn('render_preset', result)

    def test_reject_nonfinite_bool_string(self):
        for value in (float('nan'), float('inf'), True, '2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                clamp_overrides({'lights': {'intensity_mul': value}}, self.manifest, self.items)

    def test_nullable_ior_and_null_overrides(self):
        self.assertIsNone(clamp_overrides(None, self.manifest, self.items))
        self.assertIsNone(clamp_overrides({'materials': {'玻璃': {'ior': None}}}, self.manifest, self.items)['materials']['玻璃']['ior'])

    def test_schema_rejects_unknown_fields_confidence_and_types(self):
        schema = read_json(ROOT/'server'/'calibration_schema.json')
        valid = {'verdict': 'ok', 'summary': '已准', 'overrides': {'materials': [], 'lights': {'per_light': [], 'intensity_mul': 1, 'rotate_deg': 0}, 'environment': {'reflection_strength_mul': 1}, 'items': []}, 'notes': [], 'confidence': .5}
        validate_schema(valid, schema)
        for mutate in (lambda v: v.update(extra=1), lambda v: v.update(confidence=2), lambda v: v.update(verdict='no'), lambda v: v.update(notes='错误')):
            value = copy.deepcopy(valid); mutate(value)
            with self.assertRaises(ValueError):validate_schema(value, schema)

    def test_real_command_is_argument_list_readonly_and_stdin(self):
        command = command_for({'codex_command': 'codex'}, Path('schema.json'), Path('out.json'), [Path('图片 1.png')])
        self.assertIn('--ephemeral', command)
        self.assertEqual(command[command.index('-s')+1], 'read-only')
        self.assertEqual(command[command.index('-m')+1], 'gpt-6.1-sol')
        self.assertIn('model_reasoning_effort="medium"', command)
        self.assertEqual(command[-1], '-')
        self.assertEqual(command[command.index('-i')+1], '图片 1.png')


class ColorTests(unittest.TestCase):
    def test_lab_black_white_red(self):
        for rgb, expected in [((0, 0, 0), (0, 0, 0)), ((255, 255, 255), (100, 0, 0)), ((255, 0, 0), (53.2408, 80.0925, 67.2032))]:
            for actual, target in zip(srgb_to_lab(rgb), expected):self.assertAlmostEqual(actual, target, places=3)

    def test_statistics_tolerance_background_highlight_and_std(self):
        image=Image.new('RGB',(4,1));image.putdata([(255,255,255),(0,0,0),(100,100,100),(80,80,80)])
        regions=Image.new('RGB',(4,1));regions.putdata([(250,6,0),(255,0,0),(224,0,0),(0,0,0)])
        before=image.tobytes()
        result=color_statistics(image,regions,{'#FF0000':'木','#00FF00':'玻璃'})
        wood=result['木']
        self.assertEqual(wood['pixels'],2);self.assertEqual(wood['area_ratio'],.5)
        self.assertEqual(wood['mean_srgb'],[127.5]*3)
        self.assertAlmostEqual(wood['mean_lab'][0],50,places=4)
        self.assertAlmostEqual(wood['brightness_std'],127.5,places=4)
        self.assertEqual(wood['highlight_ratio'],.5)
        self.assertEqual(result['玻璃']['pixels'],0);self.assertIsNone(result['玻璃']['mean_lab'])
        self.assertEqual(image.tobytes(),before)

    def test_highlight_threshold_strictly_greater_than_245(self):
        image=Image.new('RGB',(2,1));image.putdata([(245,245,245),(246,246,246)])
        result=color_statistics(image,Image.new('RGB',(2,1),'red'),{'#FF0000':'木'})
        self.assertEqual(result['木']['highlight_ratio'],.5)

    def test_size_mismatch_fails(self):
        with self.assertRaises(ValueError):color_statistics(Image.new('RGB',(1,1)),Image.new('RGB',(2,2)),{})


@needs_product_fbx
class IsolatedTests(unittest.TestCase):
    def setUp(self):
        self.root=test_directory(self, 'cal-')
        config=read_json(ROOT/'config.json')
        config.update(calibration_auto=True, codex_command=[sys.executable,str(ROOT/'tests'/'fake_codex.py')],
                      smallshot_size={'portrait':[60,80],'landscape':[80,60]},
                      final_size={'portrait':[60,80],'landscape':[80,60]},calibration_timeout_sec=10)
        self.studio=Studio(self.root,config)
        self.addCleanup(self.studio.calibration.close)
        self.product=self.root/'产品';self.product.mkdir()
        (self.product/'柜.max').write_text('开发占位',encoding='utf-8')
        self.task=self.studio.create({'product_dir':str(self.product)})

    def finish(self,timeout=15):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            run_once(self.studio.exchange,ROOT/'tests'/'fixtures'/'L型咖啡柜_绑定.fbx')
            task=self.studio.merge(self.task['id'])['task']
            if task['ai_calibration']['state'] in ('waiting_user','failed') and not self.studio.calibration.active:
                return task
            time.sleep(.04)
        self.fail('校准流程未及时结束。')

    def test_full_state_machine_second_round_ok_accept_and_final(self):
        job=self.studio.submit(self.task['id'],{'mode':'smallshot'})
        self.assertTrue(job['want_manifest']);self.assertIsNone(job['overrides'])
        task=self.finish();c=task['ai_calibration']
        self.assertEqual(c['state'],'waiting_user');self.assertEqual(c['round'],2)
        self.assertEqual([r['verdict'] for r in c['rounds']],['adjust','adjust','ok'])
        for r in c['rounds']:
            folder=self.studio.task_dir(task['id'])/'calibration'/f"round-{r['n']}"
            for name in ('stats.json','input.json','result.json','validated.json','prompt.txt','stdout.log','stderr.log'):
                self.assertTrue((folder/name).is_file(),name)
            raw=read_json(folder/'result.json')
            saved=read_json(folder/'validated.json')
            self.assertIsInstance(raw['overrides']['materials'],list)
            self.assertIsInstance(raw['overrides']['lights']['per_light'],list)
            self.assertIsInstance(saved['overrides']['materials'],dict)
            self.assertIsInstance(saved['overrides']['lights']['per_light'],dict)
            self.assertIsInstance(saved['overrides']['items'],dict)
            self.assertIn('输出必须用数组形式', (folder/'prompt.txt').read_text(encoding='utf-8'))
        self.assertIsNone(c['accepted_overrides'])
        task=self.studio.calibration.action(task['id'],{'action':'accept','round':2})
        final=self.studio.submit(task['id'],{'mode':'final'})
        self.assertEqual(final['overrides'],task['ai_calibration']['accepted_overrides'])
        self.assertEqual(final['overrides'],c['rounds'][2]['overrides'])
        self.assertFalse(final['want_manifest'])
        self.assertEqual(read_json(self.studio.exchange/'jobs'/(final['job_id']+'.json'))['overrides'],final['overrides'])

    def test_limit_stops_before_unrendered_proposal_is_accepted(self):
        self.studio.config['calibration_max_rounds']=1
        self.studio.submit(self.task['id'],{'mode':'smallshot'})
        task=self.finish();c=task['ai_calibration']
        self.assertEqual(c['state'],'waiting_user');self.assertEqual(c['round'],1)
        self.assertEqual(c['rounds'][-1]['verdict'],'adjust')
        chosen=self.studio.calibration.action(task['id'],{'action':'accept','round':0})
        self.assertIsNone(chosen['ai_calibration']['accepted_overrides'])

    def test_codex_failure_retains_samples_allows_final(self):
        self.studio.config['codex_command']=[sys.executable,'-c','import sys;sys.exit(7)']
        self.studio.submit(self.task['id'],{'mode':'smallshot'})
        task=self.finish();c=task['ai_calibration']
        self.assertEqual(c['state'],'failed');self.assertIn('退出码 7',c['rounds'][0]['error'])
        self.assertEqual(task['items'][0]['smallshot']['state'],'done')
        self.assertIsNone(self.studio.submit(task['id'],{'mode':'final'})['overrides'])

    def test_timeout_is_failed_and_logs_retained(self):
        self.studio.config.update(codex_command=[sys.executable,'-c','import time;time.sleep(5)'],calibration_timeout_sec=.1)
        self.studio.submit(self.task['id'],{'mode':'smallshot'})
        task=self.finish();self.assertIn('超时',task['ai_calibration']['rounds'][0]['error'])
        self.assertTrue((self.studio.task_dir(task['id'])/'calibration'/'round-0'/'stderr.log').exists())

    def test_bad_schema_stops_and_retains_raw_output(self):
        self.studio.config['codex_command']=[sys.executable,'-c',"import sys,pathlib;pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('{}')"]
        self.studio.submit(self.task['id'],{'mode':'smallshot'})
        task=self.finish();self.assertEqual(task['ai_calibration']['state'],'failed')
        self.assertIn('必要字段',task['ai_calibration']['rounds'][0]['error'])

    def test_disabled_bypasses_codex_and_manifest(self):
        self.studio.update(self.task['id'],{'ai_calibration':{'enabled':False,'accepted_overrides':{'禁止':1}}})
        job=self.studio.submit(self.task['id'],{'mode':'smallshot'})
        self.assertFalse(job['want_manifest']);self.assertIsNone(job['overrides'])
        self.assertEqual(self.studio.load(self.task['id'])['ai_calibration']['state'],'idle')

    def test_single_calibration_and_edit_guard(self):
        self.studio.submit(self.task['id'],{'mode':'smallshot'})
        other=self.studio.create({'product_dir':str(self.product)})
        with self.assertRaises(StudioError):self.studio.submit(other['id'],{'mode':'smallshot'})
        modified=copy.deepcopy(self.task['items']);modified[0]['camera']['position_mm'][0]+=1
        updated=self.studio.update(self.task['id'],{'items':modified})
        self.assertEqual(updated['items'][0]['camera']['position_mm'],modified[0]['camera']['position_mm'])
        with self.assertRaises(StudioError):self.studio.calibration.action(self.task['id'],{'action':'undo'})

    def test_undo_and_retry_preserve_history_and_user_note(self):
        self.studio.config['calibration_max_rounds']=1
        self.studio.submit(self.task['id'],{'mode':'smallshot'});task=self.finish()
        self.studio.calibration.action(task['id'],{'action':'accept','round':1})
        task=self.studio.calibration.action(task['id'],{'action':'undo'})
        self.assertIsNone(task['ai_calibration']['accepted_overrides'])
        self.studio.calibration.action(task['id'],{'action':'retry','user_note':'木纹再暖一点'})
        task=self.finish();self.assertEqual(len(task['ai_calibration']['rounds']),3)
        self.assertEqual(task['ai_calibration']['user_note'],'木纹再暖一点')
        payload=read_json(self.studio.task_dir(task['id'])/'calibration'/'round-2'/'input.json')
        self.assertEqual(payload['user_note'],'木纹再暖一点')

    def test_restart_rendering_resumes_existing_job_without_duplicate(self):
        job=self.studio.submit(self.task['id'],{'mode':'smallshot'})
        self.studio.calibration.close()
        self.studio=Studio(self.root,self.studio.config)
        self.assertEqual(self.studio.jobs_for(self.task['id'])[0]['job_id'],job['job_id'])
        self.assertEqual(self.finish()['ai_calibration']['state'],'waiting_user')
        self.assertEqual(len(self.studio.jobs_for(self.task['id'])),3)

    def test_restart_thinking_marks_failed_without_codex(self):
        task=self.studio.load(self.task['id']);task['ai_calibration']['state']='thinking'
        task['ai_calibration']['rounds']=[{'n':0,'error':None}];self.studio.save(task)
        self.studio=Studio(self.root,self.studio.config)
        task=self.studio.load(task['id'])
        self.assertEqual(task['ai_calibration']['state'],'failed')
        self.assertIn('避免重复调用',task['ai_calibration']['rounds'][0]['error'])
        self.assertIsNone(self.studio.calibration.active)

    def test_render_failure_and_missing_manifest_fail_before_codex(self):
        job=self.studio.submit(self.task['id'],{'mode':'smallshot'})
        atomic_json(self.studio.exchange/'jobs'/(job['job_id']+'.status.json'),{'state':'done','items':[]})
        task=self.finish()
        self.assertEqual(task['ai_calibration']['state'],'failed')
        self.assertNotIn('result.json',[p.name for p in (self.studio.task_dir(task['id'])/'calibration'/'round-0').iterdir()])

    def test_library_build_matches_filenames_filters_and_thumbnails(self):
        history=self.root/'历史'/'产品'/'SKU';history.mkdir(parents=True)
        for name in ('摩卡红橡.jpg','胡桃木.png','奶油白.jpg','北欧橡木.jpg','幻影灰.jpg','纯黑.jpg','长虹玻璃.jpg','摩卡红橡_遮罩.jpg','红橡_TD.png','胡桃_通道.jpg'):
            Image.new('RGB',(1700,50),(120,100,80)).save(history/name)
        Image.new('RGB',(20,20)).save(history.parent/'胡桃.jpg')
        before={p:(p.stat().st_size,p.stat().st_mtime_ns) for p in history.iterdir()}
        report=build_library(self.root,[history.parent.parent])
        self.assertEqual(list(report['materials'].values()),[1]*7)
        self.assertEqual(report['style'],7);self.assertEqual(report['scanned_images'],7)
        library=self.studio.refs.read()
        for entry in library['materials'].values():
            with Image.open(self.root/entry['images'][0]['file']) as image:self.assertLessEqual(max(image.size),1600)
        entry=library['materials']['胡桃木']['images'][0]
        self.studio.refs.change_status('胡桃木',entry['file'],'approved')
        build_library(self.root,[history.parent.parent])
        self.assertEqual(self.studio.refs.read()['materials']['胡桃木']['images'][0]['status'],'approved')
        self.assertEqual(before,{p:(p.stat().st_size,p.stat().st_mtime_ns) for p in history.iterdir()})

    def test_atomic_json_retries_transient_windows_read_and_replace(self):
        path=self.root/'共享状态.json'
        atomic_json(path,{'state':'旧'})
        original_replace=os.replace
        calls=[]
        def replace(source,target):
            calls.append(1)
            if len(calls)==1:raise PermissionError('模拟 Windows 句柄占用')
            return original_replace(source,target)
        with patch('server.core.os.replace',side_effect=replace):atomic_json(path,{'state':'新'})
        self.assertEqual(len(calls),2)
        original_open=Path.open
        calls=[]
        def open_file(target,*args,**kwargs):
            calls.append(1)
            if len(calls)==1:raise PermissionError('模拟 Windows 替换中的文件')
            return original_open(target,*args,**kwargs)
        with patch.object(Path,'open',open_file):self.assertEqual(read_json(path),{'state':'新'})
        self.assertEqual(len(calls),2)

    def test_library_paths_upload_deduplicate_status_and_file_scope(self):
        image=Image.new('RGB',(20,20));stream=io.BytesIO();image.save(stream,'PNG')
        library=self.studio.refs.read()
        entry=self.studio.refs.add('奶油白','上传.png',stream.getvalue(),library)
        self.studio.refs.save(library)
        library=self.studio.refs.change_status('奶油白',entry['file'],'rejected')
        self.studio.refs.add('奶油白','上传.png',stream.getvalue(),library)
        self.assertEqual(len(library['materials']['奶油白']['images']),1)
        self.assertEqual(material_key('柜体胡桃饰面',library),'胡桃木')
        self.assertEqual(self.studio.allowed_file(entry['file']),self.root/entry['file'])
        with self.assertRaises(StudioError):self.studio.refs.change_status('奶油白',entry['file'],'错误')
        with self.assertRaises(StudioError):self.studio.refs.add('../越界','上传.png',stream.getvalue(),library)
        with self.assertRaises(StudioError):self.studio.refs.add('奶油白','坏图.png',b'not image',library)

    def test_http_library_read_upload_status_paths_and_origin(self):
        server=LocalServer(('127.0.0.1',0),handler_for(self.studio));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        image=self.root/'上传.png';Image.new('RGB',(20,20)).save(image)
        def request(path,body=None,origin=None,raw=False):
            headers={} if body is None else {'Content-Type':'application/octet-stream' if raw else 'application/json'}
            if origin:headers['Origin']=origin
            content=body if raw else json.dumps(body).encode() if body is not None else None
            with urllib.request.urlopen(urllib.request.Request(url+path,data=content,headers=headers)) as response:return json.load(response)
        try:
            self.assertIn('materials',request('/api/refs'))
            library=request('/api/refs/upload?'+urllib.parse.urlencode({'key':'奶油白','name':'上传.png'}),image.read_bytes(),raw=True)
            entry=library['materials']['奶油白']['images'][0]
            result=request('/api/refs/status',{'key':'奶油白','file':entry['file'],'status':'approved'})
            self.assertEqual(result['materials']['奶油白']['images'][0]['status'],'approved')
            result=request('/api/refs/images',{'key':'胡桃木','paths':[str(image)]})
            self.assertEqual(len(result['materials']['胡桃木']['images']),1)
            with self.assertRaises(urllib.error.HTTPError) as ctx:request('/api/refs/status',{},origin='https://example.com')
            self.assertEqual(ctx.exception.code,403)
        finally:server.shutdown();server.server_close();thread.join()

    def test_reference_selection_caps_twelve_includes_all_samples_and_rules(self):
        main = self.task['items'][0]
        items = [main]
        for no, (label, view) in enumerate((('正面', 'front'), ('侧面', 'side'), ('俯视', 'top')), 2):
            items.append({**main, 'no':no, 'label':label, 'view':view})
        self.studio.update(self.task['id'], {'items':items})
        self.studio.submit(self.task['id'],{'mode':'smallshot'});task=self.finish()
        for key in ('摩卡红橡','奶油白','长虹玻璃','style'):
            library=self.studio.refs.read()
            for n in range(3):
                stream=io.BytesIO();Image.new('RGB',(10,10),(n*40,100,50)).save(stream,'PNG')
                self.studio.refs.add(key,f'{n}.png',stream.getvalue(),library)
            self.studio.refs.save(library)
        reference=self.product/'参考图';reference.mkdir()
        Image.new('RGB',(10,10)).save(reference/'主图.png')
        task['reference_images']=[str(reference/'主图.png')]
        record=task['ai_calibration']['rounds'][-1]
        job=read_json(self.studio.exchange/'jobs'/(record['job_id']+'.json'))
        manifest=read_json(Path(job['calibration_dir'])/'scene_manifest.json')
        images,descriptions=select_images(self.studio,task,record,manifest,job)
        self.assertLessEqual(len(images),12)
        self.assertEqual(len(descriptions[0]['sources']),4)
        self.assertTrue(any('可信度低' in e['role'] for e in descriptions))
        self.assertIn('只看灯光方向',descriptions[-1]['role'])

    def test_approved_reference_and_original_images_when_budget_sufficient(self):
        task=self.studio.load(self.task['id'])
        folder=self.studio.task_dir(task['id'])/'calibration'/'round-0'
        (folder/'regions').mkdir(parents=True)
        sample=folder/'1.jpg';region=folder/'regions'/'1.png'
        Image.new('RGB',(20,20),'white').save(sample)
        Image.new('RGB',(20,20),'red').save(region)
        library=self.studio.refs.read()
        entries=[]
        for n in range(3):
            stream=io.BytesIO();Image.new('RGB',(10,10),(n*70,40,20)).save(stream,'PNG')
            entries.append(self.studio.refs.add('摩卡红橡',f'来源{n}.png',stream.getvalue(),library))
        entries[1]['status']='approved';entries[2]['status']='rejected'
        self.studio.refs.save(library)
        images,descriptions=select_images(self.studio,task,{'n':0},{'materials':[{'name':'摩卡红橡'}]},
                                          {'items':[{'no':1,'out_image':str(sample)}]})
        self.assertEqual(images[:2],[sample,region])
        self.assertIn(self.root/entries[1]['file'],images)
        self.assertNotIn(self.root/entries[0]['file'],images)
        self.assertNotIn(self.root/entries[2]['file'],images)
        self.assertIn('已认可',descriptions[-1]['role'])

    def test_transparent_thumbnail_is_white_and_refresh_preserves_approval(self):
        source=self.root/'透明来源.png'
        Image.new('RGBA',(20,20),(100,0,0,0)).save(source)
        library=self.studio.refs.read()
        entry=self.studio.refs.add('奶油白',str(source),library=library)
        entry['status']='approved'
        self.studio.refs.save(library)
        self.studio.refs.add('奶油白',str(source),library=library,refresh=True)
        self.assertEqual(entry['status'],'approved')
        with Image.open(self.root/entry['file']) as image:self.assertTrue(all(v>=250 for v in image.getpixel((0,0))))
        self.assertTrue(list((self.root/entry['file']).parent.glob('*.prev')))


if __name__=='__main__':unittest.main()
