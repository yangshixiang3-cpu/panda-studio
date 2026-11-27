"""入库与写入测试只使用临时产品。"""
import copy
import json
import math
import os
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from server.core import Studio, StudioError, atomic_json, draft_items, fit_camera, read_json, validate_camera
from server.app import LocalServer, handler_for
from test_temp_dirs import test_directory


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.root=test_directory(self, 'unit-')
        config=read_json(ROOT/'config.json');self.studio=Studio(self.root,config)
        self.addCleanup(self.studio.calibration.close)
        self.product=self.root/'临时产品';(self.product/'模型'/'二级').mkdir(parents=True)
        (self.product/'参考图').mkdir();(self.product/'sku').mkdir()
        self.max_file=self.product/'模型'/'二级'/'柜体.MAX';self.max_file.write_text('仅开发测试，不是 Max 模型',encoding='utf-8')
        self.fbx=self.product/'模型'/'二级'/'柜体.FBX';self.fbx.write_bytes(b'test')
        (self.product/'参考图'/'侧面.PNG').write_bytes(b'png')
        (self.product/'sku'/'原图.txt').write_text('保留',encoding='utf-8')
        self.task=self.studio.create({'product_dir':str(self.product)})

    def add_views(self):
        items = [self.task['items'][0]]
        for no, (label, view) in enumerate((('正面', 'front'), ('侧面', 'side'), ('俯视', 'top')), 2):
            item = {**items[0], 'no':no, 'label':label, 'view':view}
            item.pop('item_id')
            items.append(item)
        return self.studio.update(self.task['id'], {'items':items})

    def completed(self, task_id=None):
        job=self.studio.submit(task_id or self.task['id'],{'mode':'final'})
        for item in job['items']:
            for key in ('out_image','out_mask'):
                path=Path(item[key]);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'new image')
        atomic_json(self.studio.exchange/'jobs'/(job['job_id']+'.status.json'),{
            'job_id':job['job_id'],'state':'done','items':[{'no':i['no'],'state':'done','message':''} for i in job['items']]})
        return job

    def test_scan_nested_case_insensitive_models_images_and_sku(self):
        result=self.studio.scan(str(self.product))
        self.assertEqual(set(result['models']),{str(self.max_file)})  # 只列 .max，FBX 不再是模型选项
        self.assertEqual(len(result['reference_images']),1);self.assertEqual(len(result['sku_files']),1)

    def test_scan_missing_optional_folders(self):
        p=self.root/'空产品';p.mkdir();self.assertEqual(self.studio.scan(str(p))['models'],[])

    def test_draft_without_parts(self):
        item, = draft_items()
        self.assertEqual((item['no'], item['label'], item['view']), (1, '主图', 'main'))
        self.assertEqual((item['camera']['azimuth_deg'], item['camera']['elevation_deg']), (35, 12))
        self.assertEqual(item['camera']['fov_y_deg'], 27)
        self.assertFalse(item['render_open_state'])

    def test_draft_drawer_only(self):
        items=draft_items(parts=[{'name':'抽屉1','type':'drawer'}])
        self.assertEqual([i['label'] for i in items], ['主图'])
        self.assertTrue(all(i['parts'] == {'抽屉1':0.0} for i in items))

    def test_draft_each_door_type(self):
        for kind in ('door','flap','slide'):
            with self.subTest(kind=kind):
                self.assertEqual([i['label'] for i in draft_items(parts=[{'name':'柜门','type':kind}])],
                                 ['主图'])

    def test_draft_both_parts_continuous_numbers_and_closed(self):
        items=draft_items(parts=[{'name':'抽屉','type':'drawer'},{'name':'门','type':'door'}])
        self.assertEqual([i['no'] for i in items], [1])
        self.assertTrue(all(i['parts']=={'抽屉':0.,'门':0.} for i in items))

    def test_portrait_landscape_drafting(self):
        self.assertEqual(draft_items((3000,500,400))[0]['orientation'],'landscape')
        self.assertEqual(draft_items((400,2000,400))[0]['orientation'],'portrait')

    def test_create_task_schema_and_paths(self):
        task=read_json(self.studio.task_dir(self.task['id'])/'task.json')
        self.assertEqual(task['product_name'],'临时产品');self.assertEqual(task['model_max'],str(self.max_file))
        self.assertEqual([item['view'] for item in task['items']], ['main'])
        self.assertTrue(Path(task['preview_fbx']).is_absolute());self.assertIsNone(task['delivered'])

    def test_atomic_write_uses_complete_temp_then_replace_without_backup(self):
        p=self.root/'atomic.json';atomic_json(p,{'旧':1})
        import os
        original=os.replace
        def observe(src,dst):
            self.assertTrue(str(src).endswith('.tmp'));self.assertEqual(read_json(src),{'新':2})
            self.assertEqual(read_json(dst),{'旧':1});original(src,dst)
        with patch('server.core.os.replace',side_effect=observe):atomic_json(p,{'新':2})
        self.assertEqual(list(self.root.glob('atomic.json*.bak')),[])
        self.assertEqual(read_json(p),{'新':2})
        self.assertEqual(list(self.root.glob('*.tmp')),[])

    def test_job_files_immutable(self):
        p=self.studio.exchange/'jobs'/'immutable.json';atomic_json(p,{'a':1},immutable=True)
        with self.assertRaises(StudioError):atomic_json(p,{'a':2},immutable=True)
        self.assertEqual(read_json(p),{'a':1})

    def test_export_preview_protocol_and_deduplication(self):
        first=self.studio.preview(self.task['id'],{'source':'export'})['job']
        second=self.studio.preview(self.task['id'],{'source':'export'})['job']
        self.assertEqual(first,second);self.assertEqual(first['type'],'export_preview')
        self.assertTrue(all(Path(first[k]).is_absolute() for k in ('max_file','out_fbx','out_bbox')))

    def test_render_protocol_sizes_mask_time_and_no_parts(self):
        small=self.studio.submit(self.task['id'],{'mode':'smallshot'})
        final=self.studio.submit(self.task['id'],{'mode':'final'})
        self.assertEqual(small['time_limit_sec'],25);self.assertIsNone(final['time_limit_sec'])
        self.assertTrue(all(i['out_mask'] is None for i in small['items']))
        self.assertTrue(all(i['out_mask'].endswith('_遮罩_ID.jpg') for i in final['items']))
        self.assertTrue(all('parts' not in i for i in final['items']))
        for i in small['items']:
            self.assertIn([i['width'],i['height']],[[600,800],[800,600]])

    def test_status_merge_item_states_and_job_failure(self):
        self.add_views()
        job=self.studio.submit(self.task['id'],{'mode':'smallshot'})
        atomic_json(self.studio.exchange/'jobs'/(job['job_id']+'.status.json'),{
            'state':'failed','items':[{'no':1,'state':'done'},{'no':2,'state':'rendering'},{'no':3,'state':'pending'}],'message':'测试失败'})
        task=self.studio.merge(self.task['id'])['task']
        self.assertEqual([i['smallshot']['state'] for i in task['items']],['done','rendering','queued','failed'])
        self.assertEqual(read_json(self.studio.task_dir(task['id'])/'task.json'),task)

    def test_waiting_lock_merges_as_queued(self):
        job=self.studio.submit(self.task['id'],{'mode':'smallshot'})
        atomic_json(self.studio.exchange/'jobs'/(job['job_id']+'.status.json'),{'state':'waiting_lock','items':[]})
        self.assertTrue(all(i['smallshot']['state']=='queued' for i in self.studio.merge(self.task['id'])['task']['items']))

    def test_old_job_status_does_not_overwrite_new_job(self):
        first=self.completed();second=self.studio.submit(self.task['id'],{'mode':'final'})
        merged=self.studio.merge(self.task['id'])['task']
        self.assertTrue(all(i['final']['job_id']==second['job_id'] and i['final']['state']=='queued' for i in merged['items']))

    def test_server_ignores_client_status_and_forces_closed_render(self):
        task=copy.deepcopy(self.task);task['items'][0]['final']['state']='done';task['items'][0]['render_open_state']=True
        saved=self.studio.update(task['id'],{'items':task['items']})
        self.assertEqual(saved['items'][0]['final']['state'],'none');self.assertFalse(saved['items'][0]['render_open_state'])

    def test_position_and_target_authoritative(self):
        value=copy.deepcopy(self.task['items'][0]['camera']);value['azimuth_deg']=-100
        self.assertAlmostEqual(validate_camera(value)['azimuth_deg'],35)

    def test_reorder_renumbers_and_preserves_results(self):
        self.add_views()
        self.completed();task=self.studio.merge(self.task['id'])['task'];task['items'].reverse()
        updated=self.studio.update(task['id'],{'items':task['items']})
        self.assertEqual([i['no'] for i in updated['items']],[1,2,3,4]);self.assertEqual(updated['items'][0]['label'],'俯视')
        self.assertTrue(all(i['final']['state']=='done' for i in updated['items']))
        self.assertEqual([i['final']['file'] for i in updated['items']],[f'final/{n}.jpg' for n in range(1,5)])

    def test_initial_draft_does_not_overwrite_user_adjustments(self):
        task=copy.deepcopy(self.task);task['items'][0]['label']='我的主图';self.studio.update(task['id'],{'items':task['items']})
        result=self.studio.draft(task['id'],{'initial':True,'size_mm':[500,500,500],'parts':[]})
        self.assertEqual(result['items'][0]['label'],'我的主图')

    def test_initial_draft_refines_real_size_and_parts(self):
        result=self.studio.draft(self.task['id'],{'initial':True,'size_mm':[2000,2300,1600],
            'parts':[{'name':'抽屉1','type':'drawer'},{'name':'移门1','type':'slide'}]})
        self.assertEqual([i['label'] for i in result['items']], ['主图'])
        self.assertTrue(all(i['parts']=={'抽屉1':0.0,'移门1':0.0} for i in result['items']))
        self.assertNotEqual(result['items'][0]['camera']['distance_mm'],self.task['items'][0]['camera']['distance_mm'])

    def test_fbx_cannot_be_submitted_as_max_model(self):
        with self.assertRaises(StudioError):self.studio.create({'product_dir':str(self.product),'model_max':str(self.fbx)})

    def test_file_allowlist_and_traversal(self):
        web=self.root/'web';web.mkdir();(web/'index.html').write_text('网页',encoding='utf-8')
        self.assertEqual(self.studio.allowed_file(self.max_file,self.task['id']),self.max_file)
        self.assertEqual(self.studio.allowed_file(web/'index.html'),web/'index.html')
        for p in ('C:/Windows/win.ini',self.product/'私密.txt',web/'..'/'config.json',self.fbx):
            with self.subTest(p=p),self.assertRaises(StudioError) as context:self.studio.allowed_file(p,self.task['id'])
            self.assertEqual(context.exception.status,403)

    def test_other_product_files_not_allowed(self):
        other=self.root/'其他产品'/'模型';other.mkdir(parents=True);p=other/'a.fbx';p.write_bytes(b'x')
        with self.assertRaises(StudioError) as context:self.studio.allowed_file(p,self.task['id'])
        self.assertEqual(context.exception.status,403)

    def test_delivery_requires_exact_plan_token(self):
        self.completed();self.assertFalse((self.product/'sku'/'1.jpg').exists())
        with self.assertRaises(StudioError):self.studio.deliver(self.task['id'],'wrong')
        self.assertFalse((self.product/'sku'/'1.jpg').exists())

    def test_changed_destination_invalidates_confirmation(self):
        self.completed();plan=self.studio.delivery_plan(self.task['id'])
        target=self.product/'sku'/'1.jpg';target.write_bytes(b'newly added old file')
        with self.assertRaises(StudioError):self.studio.deliver(self.task['id'],plan['token'])
        self.assertEqual(target.read_bytes(),b'newly added old file')

    def test_part_preview_values_do_not_change_render_result(self):
        self.completed();task=self.studio.merge(self.task['id'])['task'];task['items'][0]['parts']={'抽屉':.7}
        result=self.studio.update(task['id'],{'items':task['items']})
        self.assertEqual(result['items'][0]['final']['state'],'done')
        self.assertFalse(result['items'][0]['render_open_state'])

    def test_delivery_twice_archives_old_files_manifest_and_keeps_unrelated(self):
        self.completed();sku=self.product/'sku';(sku/'1.jpg').write_bytes(b'old image')
        (sku/'白底清单.json').write_text('旧清单',encoding='utf-8')
        plan=self.studio.delivery_plan(self.task['id']);self.assertEqual(plan['files'][0]['action'],'archive')
        result=self.studio.deliver(self.task['id'],plan['token'])
        self.assertEqual((Path(result['archive_dir'])/'1.jpg').read_bytes(),b'old image')
        self.assertEqual((Path(result['archive_dir'])/'白底清单.json').read_text(encoding='utf-8'),'旧清单')
        self.assertEqual((sku/'原图.txt').read_text(encoding='utf-8'),'保留')
        manifest=read_json(sku/'白底清单.json');self.assertEqual(manifest[0]['task_id'],self.task['id'])
        self.assertTrue(all(k in manifest[0] for k in ('no','label','view','orientation','camera','model_max','task_id','delivered_at')))
        second=self.studio.delivery_plan(self.task['id']);again=self.studio.deliver(self.task['id'],second['token'])
        self.assertNotEqual(result['archive_dir'],again['archive_dir']);self.assertTrue((Path(again['archive_dir'])/'1_遮罩_ID.jpg').is_file())
        self.assertTrue((sku/'1.jpg').is_file());self.assertTrue((sku/'1_遮罩_ID.jpg').is_file());self.assertTrue((sku/'白底清单.json').is_file())
        self.assertIsNotNone(self.studio.load(self.task['id'])['delivered'])

    def test_disabled_items_not_delivered(self):
        self.add_views()
        self.completed();task=self.studio.merge(self.task['id'])['task'];task['items'][1]['enabled']=False
        self.studio.update(task['id'],{'items':task['items']});plan=self.studio.delivery_plan(task['id'])
        self.assertNotIn('2.jpg',[f['name'] for f in plan['files']])

    def test_missing_mask_blocks_delivery_before_writing(self):
        job=self.studio.submit(self.task['id'],{'mode':'final'})
        p=Path(job['items'][0]['out_image']);p.parent.mkdir(parents=True);p.write_bytes(b'x')
        atomic_json(self.studio.exchange/'jobs'/(job['job_id']+'.status.json'),{'state':'done','items':[{'no':1,'state':'done'}]})
        with self.assertRaises(StudioError):self.studio.delivery_plan(self.task['id'])
        self.assertFalse((self.product/'sku'/'1.jpg').exists())

    def test_http_forbidden_path_and_external_origin(self):
        server=LocalServer(('127.0.0.1',0),handler_for(self.studio));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(base+'/api/file?'+urllib.parse.urlencode({'path':'C:\\Windows\\win.ini','task_id':self.task['id']}))
            self.assertEqual(context.exception.code,403)
            request=urllib.request.Request(base+'/api/tasks',data=b'{}',headers={'Content-Type':'application/json','Origin':'https://example.com'})
            with self.assertRaises(urllib.error.HTTPError) as context:urllib.request.urlopen(request)
            self.assertEqual(context.exception.code,403)
        finally:server.shutdown();server.server_close();thread.join()

    def variants(self):
        return [
            {'id':'oak', 'name':'橡木', 'nodes':['橡木门', '橡木柜体'],
             'bbox_mm':{'min':[-600, -400, -200], 'max':[600, 400, 200]}},
            {'id':'white_2', 'name':'白色', 'nodes':['白色门', '白色柜体'],
             'bbox_mm':{'min':[800, -400, -200], 'max':[2000, 400, 200]}},
        ]

    def prepare_preview(self):
        job = self.studio.preview(self.task['id'], {'source':'export'})['job']
        fbx = Path(job['out_fbx'])
        fbx.parent.mkdir()
        fbx.write_bytes(b'whole-scene preview')
        bbox = {'units':'mm', 'max_world_bbox_mm':{'min':[0, 0, 0], 'max':[2600, 400, 800]},
                'size_mm':{'x':2600, 'y_up':800, 'z_depth':400}, 'mesh_count':4}
        atomic_json(job['out_bbox'], bbox)
        atomic_json(self.studio.exchange/'jobs'/f"{job['job_id']}.status.json", {'state':'done'})
        return bbox

    def test_variant_validation_rejects_invalid_requests_without_saving(self):
        valid = self.variants()
        invalid = [
            [], valid * 11, {}, [None],
            *[[{**valid[0], 'id':value}] for value in ('', '中文', 'a-b', 'X'*41, 1)],
            [{**valid[0], 'id':valid[1]['id']}, valid[1]],
            *[[{**valid[0], 'name':value}] for value in (' <>| ', 'X'*41, None, 'COM1.foo')],
            [{**valid[0], 'name':'Oak'}, {**valid[1], 'name':'o<ak'}],
            *[[{**valid[0], 'nodes':value}] for value in ([], '门', [''], [1], ['X'*256], ['门']*2001, ['\ud800'])],
            [valid[0], {**valid[1], 'nodes':[valid[0]['nodes'][0]]}],
            *[[{**valid[0], 'bbox_mm':value}] for value in (None, {}, {'min':[0, 0], 'max':[1, 1, 1]},
               *[{'min':[value, 0, 0], 'max':[1, 1, 1]}
                 for value in (math.nan, math.inf, -math.inf, True, '0', 10**500)],
               {'min':[2, 0, 0], 'max':[1, 1, 1]})],
        ]
        task_id = self.task['id']
        for index, variants in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(StudioError) as error:
                self.studio.set_variants(task_id, {'variants':variants, 'active':'oak'})
            self.assertEqual(error.exception.status, 400)
        for active in (None, 'unknown', []):
            with self.subTest(active=active), self.assertRaises(StudioError):
                self.studio.set_variants(task_id, {'variants':valid, 'active':active})
        self.assertNotIn('variants', self.studio.load(task_id))

    def test_variant_http_routes_reuse_preview_and_list_fields(self):
        task_id = self.task['id']
        variants = self.variants()
        variants[0]['name'] = ' 橡/木: '
        self.studio.update(task_id, {'lighting':{'enabled':True, 'azimuth_deg':-55},
                                     'ai_calibration':{'enabled':False}, 'output_preset':'6K',
                                     'preview_rotation_deg':90})
        task = self.studio.load(task_id)
        task['ai_calibration'].update(state='accepted', round=2, user_note='只留在原任务')
        self.studio.save(task)
        server = LocalServer(('127.0.0.1', 0), handler_for(self.studio))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def call(path, data=None):
            body = json.dumps(data).encode() if data is not None else None
            request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/tasks{path}',
                                             data=body, headers={'Content-Type':'application/json'})
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, json.load(error)

        _, listing = call('')
        self.assertEqual({key:listing['tasks'][0][key] for key in ('variant_name', 'variant_group', 'variant_count')},
                         {'variant_name':None, 'variant_group':None, 'variant_count':0})
        status, saved = call(f'/{task_id}/variants', {'variants':variants, 'active':'oak'})
        self.assertEqual(status, 200)
        self.assertEqual(saved['task']['variant']['name'], '橡木')
        self.assertEqual(saved['task']['variant_group'], task_id)
        self.assertEqual((saved['siblings'], saved['orphans']), ([], []))
        switch = f'/{task_id}/switch-variant'
        self.assertEqual(call(switch, {'variant_id':'white_2'}),
                         (409, {'error':'请等预览模型准备好再切换柜子'}))
        self.assertEqual(call(switch, {'variant_id':'oak'}), (200, {'task_id':task_id}))
        self.assertEqual(call(switch, {'variant_id':'unknown'})[0], 404)
        bbox = self.prepare_preview()
        status, result = call(switch, {'variant_id':'white_2'})
        self.assertEqual(status, 200)
        sibling_id = result['task_id']
        self.assertNotEqual(sibling_id, task_id)
        self.assertEqual(call(switch, {'variant_id':'white_2'}), (200, result))
        self.assertEqual(call(f'/{sibling_id}/switch-variant', {'variant_id':'oak'}), (200, {'task_id':task_id}))
        status, bundle = call(f'/{sibling_id}')
        self.assertEqual(status, 200)
        self.assertEqual(bundle['preview'], {'state':'done', 'message':'', 'bbox':bbox})
        self.assertEqual(bundle['jobs'], [])
        sibling = bundle['task']
        self.assertEqual(Path(sibling['preview_fbx']).read_bytes(), Path(task['preview_fbx']).read_bytes())
        for key in ('product_dir', 'product_name', 'model_max', 'reference_images', 'lighting', 'output_preset'):
            self.assertEqual(sibling[key], task[key])
        self.assertEqual(sibling['variant'], variants[1])
        self.assertEqual(sibling['variant_group'], task_id)
        self.assertEqual(sibling['preview_rotation_deg'], 0)
        self.assertIsNone(sibling['delivered'])
        self.assertEqual([i['view'] for i in sibling['items']], ['main'])
        self.assertEqual(sibling['items'][0]['camera'], draft_items()[0]['camera'])
        self.assertNotEqual(sibling['items'][0]['item_id'], task['items'][0]['item_id'])
        self.assertEqual((sibling['ai_calibration']['enabled'], sibling['ai_calibration']['state'],
                          sibling['ai_calibration']['round'], sibling['ai_calibration']['user_note']),
                         (False, 'idle', 0, ''))
        self.assertIsNone(call(f'/{sibling_id}/preview', {'source':'export'})[1]['job'])
        self.assertEqual(len(self.studio.jobs_for(task_id)), 1)
        self.assertEqual(self.studio.jobs_for(sibling_id), [])
        self.assertEqual(call(f'/{sibling_id}/variant-name', {'variant_id':'white_2', 'name':'白*色新款'})[0], 200)
        self.assertEqual(self.studio.load(task_id)['variants'][1]['name'], '白色新款')
        self.assertEqual(self.studio.load(sibling_id)['variant']['name'], '白色新款')
        rows = {row['id']:row for row in call('')[1]['tasks']}
        self.assertEqual((rows[task_id]['variant_name'], rows[sibling_id]['variant_name']), ('橡木', '白色新款'))
        self.assertEqual({(row['variant_group'], row['variant_count']) for row in rows.values()}, {(task_id, 2)})

    def test_reidentify_updates_siblings_and_keeps_orphan_selection(self):
        task_id = self.task['id']
        variants = self.variants()
        self.studio.set_variants(task_id, {'variants':variants, 'active':'oak'})
        self.prepare_preview()
        sibling_id = self.studio.switch_variant(task_id, {'variant_id':'white_2'})['task_id']
        link = {'task_id':sibling_id, 'variant_id':'white_2'}
        job = self.studio.submit(sibling_id, {'mode':'smallshot'})
        variants[1].update(name='浅白', nodes=['新柜体'])
        for state in ('queued', 'waiting_lock', 'running'):
            if state != 'queued':
                atomic_json(self.studio.exchange/'jobs'/f"{job['job_id']}.status.json", {'state':state})
            with self.subTest(state=state), self.assertRaises(StudioError) as error:
                self.studio.set_variants(task_id, {'variants':variants, 'active':'oak'})
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(self.studio.load(sibling_id)['variant']['name'], '白色')
        atomic_json(self.studio.exchange/'jobs'/f"{job['job_id']}.status.json", {'state':'done',
                                                                               'items':[{'no':1, 'state':'done'}]})
        updated = self.studio.set_variants(task_id, {'variants':variants, 'active':'oak'})
        self.assertEqual(updated['siblings'], [link])
        self.assertEqual(updated['orphans'], [])
        sibling = self.studio.load(sibling_id)
        self.assertEqual(sibling['variants'], variants)
        self.assertEqual(sibling['variant'], variants[1])
        self.assertEqual(sibling['items'][0]['smallshot']['state'], 'none')
        updated = self.studio.set_variants(task_id, {'variants':variants[:1], 'active':'oak'})
        self.assertIsNone(updated['task']['variant'])
        self.assertEqual(updated['orphans'], [link])
        sibling = self.studio.load(sibling_id)
        self.assertEqual(sibling['variants'], variants[:1])
        self.assertEqual(sibling['variant'], variants[1])
        self.assertEqual(self.studio.switch_variant(task_id, {'variant_id':'white_2'}), {'task_id':sibling_id})
        with self.assertRaises(StudioError) as error:
            self.studio.set_variants(task_id, {'variants':variants, 'active':'white_2'})
        self.assertEqual(error.exception.status, 409)
        variants[1].update(name='暖白', nodes=['新柜门'])
        updated = self.studio.set_variants(task_id, {'variants':variants, 'active':'oak'})
        self.assertEqual(updated['orphans'], [])
        self.assertEqual(self.studio.load(sibling_id)['variant'], variants[1])

    def test_variant_render_payload_filters_normal_and_calibration_jobs(self):
        task_id = self.task['id']
        variants = self.variants()
        self.studio.set_variants(task_id, {'variants':variants, 'active':'oak'})
        small = self.studio.submit(task_id, {'mode':'smallshot'})
        final = self.studio.submit(task_id, {'mode':'final'})
        calibration = self.studio._submit_render(self.studio.load(task_id), 'smallshot', round_no=0)
        for job in (small, final, calibration):
            saved = read_json(self.studio.exchange/'jobs'/f"{job['job_id']}.json")
            self.assertEqual(saved['keep_nodes'], variants[0]['nodes'])
            self.assertEqual(saved['variant_name'], '橡木')
            self.assertEqual(Path(saved['items'][0]['out_image']).name, '1.jpg')
        self.assertTrue(calibration['want_manifest'])

    def test_variant_delivery_prefixes_and_keeps_sibling_assets(self):
        task_id = self.task['id']
        variants = self.variants()
        self.studio.update(task_id, {'lighting':{'enabled':True, 'azimuth_deg':45}})
        self.studio.set_variants(task_id, {'variants':variants, 'active':'oak'})
        self.prepare_preview()
        sibling_id = self.studio.switch_variant(task_id, {'variant_id':'white_2'})['task_id']
        sku = self.product/'sku'
        for member_id, name in ((task_id, '橡木'), (sibling_id, '白色')):
            self.completed(member_id)
            folder = self.studio.task_dir(member_id)
            light = folder/'lighting'/'1.png'
            light.parent.mkdir()
            light.write_bytes(name.encode())
            (folder/'final'/'1.jpg').write_bytes(name.encode())
            plan = self.studio.delivery_plan(member_id)
            expected = [f'{name}_1.jpg', f'{name}_1_遮罩_ID.jpg', f'光向参考/{name}_1.png', f'白底清单_{name}.json']
            self.assertEqual([file['name'] for file in plan['files']], expected)
            self.assertTrue(all(file['action'] == 'new' for file in plan['files']))
            self.assertEqual(self.studio.deliver(member_id, plan['token'])['files'], expected)
            record, = read_json(sku/expected[-1])
            self.assertEqual([record[key] for key in ('image_file', 'mask_file', 'lighting_file')], expected[:3])
        for variant in variants:
            name = variant['name']
            self.assertEqual((sku/f'{name}_1.jpg').read_bytes(), name.encode())
            self.assertTrue((sku/f'{name}_1_遮罩_ID.jpg').is_file())
            self.assertEqual((self.product/'光向参考'/f'{name}_1.png').read_bytes(), name.encode())
        self.assertFalse((sku/'1.jpg').exists())
        with self.assertRaises(StudioError):
            self.studio.rename_variant(sibling_id, {'variant_id':'white_2', 'name':'橡木'})
        self.studio.rename_variant(task_id, {'variant_id':'oak', 'name':'深橡木'})
        self.studio.rename_variant(sibling_id, {'variant_id':'white_2', 'name':'橡木'})
        with self.assertRaisesRegex(StudioError, '已有同组任务的入库记录'):
            self.studio.delivery_plan(sibling_id)

    def test_recognized_single_variant_uses_legacy_render_and_delivery_names(self):
        task_id = self.task['id']
        variants = self.variants()[:1]
        saved = self.studio.set_variants(task_id, {'variants':variants, 'active':'oak'})
        self.assertEqual(saved['task']['variants'], variants)
        self.assertIsNone(saved['task']['variant'])
        self.assertIsNone(saved['task'].get('variant_group'))
        self.assertNotIn('keep_nodes', self.completed())
        self.studio.update(task_id, {'lighting':{'enabled':True, 'azimuth_deg':45}})
        self.completed()
        light = self.studio.task_dir(task_id)/'lighting'/'1.png'
        light.parent.mkdir()
        light.write_bytes(b'light direction')
        plan = self.studio.delivery_plan(task_id)
        self.assertEqual([file['name'] for file in plan['files']],
                         ['1.jpg', '1_遮罩_ID.jpg', '光向参考/1.png', '白底清单.json'])
        self.studio.deliver(task_id, plan['token'])
        record, = read_json(self.product/'sku'/'白底清单.json')
        self.assertNotIn('image_file', record)
        self.assertEqual((self.product/'光向参考'/'1.png').read_bytes(), b'light direction')


if __name__=='__main__':unittest.main()
