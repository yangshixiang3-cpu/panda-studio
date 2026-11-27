"""桌面路径、递归扫描、参考图、端口归属回归；写入仅在 tests/tmp。"""
import base64
import importlib.machinery
import importlib.util
import json
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.core import Studio, StudioError, SKIP_MODEL_DIRS, read_json
from server.paths import resolve_input_path, plan_inputs
from server.app import LocalServer, handler_for
from test_temp_dirs import test_directory

loader = importlib.machinery.SourceFileLoader('whitebg_desktop', str(ROOT/'desktop.pyw'))
spec = importlib.util.spec_from_loader(loader.name, loader)
desktop = importlib.util.module_from_spec(spec)
loader.exec_module(desktop)
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlSIAAAAASUVORK5CYII=')


class PathTests(unittest.TestCase):
    def test_product_folder(self):
        self.assertEqual(resolve_input_path(r'C:\产品', True), {'kind':'product','product_dir':r'C:\产品','selected_model':''})

    def test_special_subfolder(self):
        for name in ('模型','参考图','SKU','视角图'):
            with self.subTest(name=name):
                self.assertEqual(resolve_input_path('C:\\产品\\'+name, True)['product_dir'], r'C:\产品')

    def test_nested_model(self):
        result = resolve_input_path(r'C:\产品\模型\日期\x.max')
        self.assertEqual(result['product_dir'], r'C:\产品')
        self.assertEqual(result['selected_model'], r'C:\产品\模型\日期\x.max')

    def test_root_model(self):
        self.assertEqual(resolve_input_path(r'C:\产品\x.max')['product_dir'], r'C:\产品')

    def test_image(self):
        for ext in ('.jpg','.JPEG','.png','.webp'):
            self.assertEqual(resolve_input_path('C:\\产品\\图'+ext)['kind'], 'image')

    def test_nearest_model_folder_and_unc_max_fbx_ignored(self):
        self.assertEqual(resolve_input_path(r'\\共享\产品\模型\副本\模型\x.MAX')['product_dir'], r'\\共享\产品\模型\副本')
        # 只导入 Max 文件：FBX 不再当作模型
        self.assertEqual(resolve_input_path(r'\\共享\产品\模型\x.FBX')['kind'], 'ignored')

    def test_missing_and_ignored(self):
        for value in ('', None, 'x.max'):
            self.assertEqual(resolve_input_path(value)['kind'], 'missing')
        self.assertEqual(resolve_input_path(r'C:\产品\x.txt')['kind'], 'ignored')

    def test_multiple_inputs_priority_and_images(self):
        plan = plan_inputs([{'path':r'C:\产品\x.max'}, {'path':r'C:\别的产品','is_directory':True},
                            {'path':r'C:\图片.png'}, {'path':None}, {'path':r'C:\说明.txt'}])
        self.assertEqual(plan['product']['selected_model'], r'C:\产品\x.max')
        self.assertEqual(plan['images'], [r'C:\图片.png'])
        self.assertEqual((plan['ignored'], plan['missing']), (2,1))


class DesktopStudioTests(unittest.TestCase):
    def setUp(self):
        self.root = test_directory(self, 'desk-')
        self.product = self.root/'产品'
        self.product.mkdir()
        self.studio = Studio(self.root, read_json(ROOT/'config.json'))
        self.addCleanup(self.studio.calibration.close)
        self.model = self.write('x.max', '测试模型'.encode())

    def write(self, relative, content=b'test'):
        target = self.product/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def create(self, **kwargs):
        return self.studio.create({'product_dir':str(self.product), **kwargs})

    def test_scan_root_and_priority_and_depth(self):
        preferred = self.write('批次/模型/日期/z.max')
        at_five = self.write('a/b/c/d/e/five.max')
        self.write('a/b/c/d/e/f/too-deep.max')
        self.write('批次/模型/日期/不导入.FBX')
        result = self.studio.scan(str(self.product))
        self.assertEqual(result['models'][0], str(preferred))
        self.assertEqual(set(result['models']), {str(self.model),str(preferred),str(at_five)})

    def test_all_excluded_folders_case_insensitive(self):
        for name in SKIP_MODEL_DIRS | {'.隐藏'}:
            self.write(name.upper()+'/bad.max')
        self.assertEqual(self.studio.scan(str(self.product))['models'], [str(self.model)])

    def test_references_at_any_depth_and_sku_exists(self):
        ref = self.write('maps/一/二/三/四/五/参考图/图.PNG', PNG)
        result = self.studio.scan(str(self.product))
        self.assertEqual(result['reference_images'], [str(ref)])
        self.assertFalse(result['sku_exists'])
        self.write('sku/已有.jpg', PNG)
        result = self.studio.scan(str(self.product))
        self.assertTrue(result['sku_exists'])
        self.assertEqual(len(result['sku_files']), 1)

    def test_fbx_is_not_a_model_option(self):
        fbx = self.write('独立.FBX')
        self.assertNotIn(str(fbx), self.studio.scan(str(self.product))['models'])
        with self.assertRaises(StudioError):
            self.create(model_file=str(fbx))
        task = self.create()
        with self.assertRaisesRegex(StudioError, 'FBX'):
            self.studio.preview(task['id'], {'source': 'file', 'path': str(fbx)})

    def test_delivery_dir_per_product(self):
        target = self.product.parent/'交付'
        target.mkdir()
        result = self.studio.set_delivery_dir(str(self.product), str(target))
        self.assertEqual((result['delivery_dir'], result['delivery_custom']), (str(target.resolve()), True))
        self.assertEqual(self.studio.scan(str(self.product))['delivery_dir'], str(target.resolve()))
        with self.assertRaisesRegex(StudioError, '找不到'):
            self.studio.set_delivery_dir(str(self.product), str(self.product.parent/'没有'))
        with self.assertRaises(StudioError):
            self.studio.set_delivery_dir(str(self.product), str(self.studio.exchange))
        result = self.studio.set_delivery_dir(str(self.product), '')
        self.assertEqual((result['delivery_dir'], result['delivery_custom']), (str(self.product.resolve()/'sku'), False))

    def test_selected_max_overrides_default(self):
        chosen = self.write('z.max')
        self.assertEqual(self.create(model_file=str(chosen))['model_max'], str(chosen))

    def test_model_whitelist_denies_adjacent_files_and_excluded_models(self):
        hidden = self.write('模型/说明.jpg', PNG)
        excluded = self.write('maps/贴图.max')
        root_image = self.write('x.jpg', PNG)
        task = self.create()
        self.assertEqual(self.studio.allowed_file(str(self.model), task['id']), self.model)
        for path in (hidden, excluded, root_image, Path('C:/Windows/win.ini')):
            with self.subTest(path=path), self.assertRaises(StudioError) as error:
                self.studio.allowed_file(str(path), task['id'])
            self.assertEqual(error.exception.status, 403)

    def test_nested_reference_authorized_without_adjacent_text(self):
        ref = self.write('批次/参考图/图.png', PNG)
        other = self.write('批次/参考图/说明.txt')
        task = self.create()
        self.assertEqual(self.studio.allowed_file(str(ref), task['id']), ref)
        with self.assertRaises(StudioError):self.studio.allowed_file(str(other), task['id'])

    def test_copy_reference_preserves_source_and_renames_duplicates(self):
        source = self.write('图.png', PNG)
        task = self.create()
        updated = self.studio.add_reference_paths(task['id'], [str(source),str(source)])
        paths = updated['reference_images']
        self.assertEqual([Path(p).name for p in paths], ['图.png','图 (2).png'])
        self.assertTrue(all(Path(p).parent == self.studio.task_dir(task['id'])/'references' for p in paths))
        self.assertTrue(all(Path(p).read_bytes() == PNG for p in paths))
        self.assertEqual(source.read_bytes(), PNG)
        self.assertEqual(self.studio.load(task['id'])['reference_images'], paths)

    def test_reference_rejects_wrong_type_and_signature(self):
        task = self.create()
        for name, content in [('x.max',PNG),('x.png',b'not an image'),('x.jpg',PNG)]:
            with self.subTest(name=name), self.assertRaises(StudioError) as error:
                self.studio.add_reference(task['id'], name, content)
            self.assertEqual(error.exception.status,415)
        self.assertEqual(self.studio.load(task['id'])['reference_images'], [])

    def test_reference_size_boundary_and_batch_preflight(self):
        task = self.create()
        with patch('server.core.MAX_REFERENCE_BYTES',len(PNG)):
            self.studio.add_reference(task['id'],'ok.png',PNG)
            with self.assertRaises(StudioError) as error:self.studio.add_reference(task['id'],'large.png',PNG+b'x')
            self.assertEqual(error.exception.status,413)
        good = self.write('好.png',PNG)
        bad = self.write('坏.png',b'invalid image')
        with self.assertRaises(StudioError):self.studio.add_reference_paths(task['id'],[str(good),str(bad)])
        self.assertEqual(len(self.studio.load(task['id'])['reference_images']),1)

    def test_http_upload_path_copy_health_and_limits(self):
        task = self.create()
        server, thread = desktop.start_service(self.studio, ('127.0.0.1',0))
        url = f'http://127.0.0.1:{server.server_port}/'
        def upload(content, name='图.png', origin=None):
            headers = {'Content-Type':'image/png'}
            if origin:headers['Origin']=origin
            request = urllib.request.Request(url+f'api/tasks/{task["id"]}/references/upload?'+urllib.parse.urlencode({'name':name}),data=content,headers=headers)
            return json.load(urllib.request.urlopen(request))
        try:
            self.assertTrue(desktop.is_studio_service(url))
            result = upload(PNG)
            self.assertEqual(len(result['task']['reference_images']),1)
            with self.assertRaises(urllib.error.HTTPError) as error:upload(PNG,origin='https://example.com')
            self.assertEqual(error.exception.code,403)
            with patch('server.app.MAX_REFERENCE_BYTES',len(PNG)-1):
                with self.assertRaises(urllib.error.HTTPError) as error:upload(PNG)
                self.assertEqual(error.exception.code,413)
            source = self.write('原图.webp',b'RIFF0000WEBPtest')
            request = urllib.request.Request(url+f'api/tasks/{task["id"]}/references',data=json.dumps({'paths':[str(source)]}).encode(),headers={'Content-Type':'application/json'})
            self.assertEqual(len(json.load(urllib.request.urlopen(request))['task']['reference_images']),2)
        finally:desktop.stop_service(server, thread)
        self.assertFalse(thread.is_alive())

    def test_own_service_reused_and_not_stopped(self):
        server, thread = desktop.start_service(self.studio, ('127.0.0.1',0))
        try:
            owned, worker = desktop.start_service(self.studio, server.server_address)
            self.assertIsNone(owned)
            self.assertIsNone(worker)
            desktop.stop_service(owned,worker)
            self.assertTrue(desktop.is_studio_service(f'http://127.0.0.1:{server.server_port}/'))
        finally:desktop.stop_service(server,thread)

    def test_unrelated_service_rejected(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Other(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(b'{"service":"other"}')
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Other)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with self.assertRaisesRegex(RuntimeError,'被别的程序占用'):
                desktop.start_service(self.studio,server.server_address)
        finally:server.shutdown();server.server_close();thread.join()

    def test_health_rejects_non_object_json(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'[]'
        with patch.object(desktop.urllib.request, 'urlopen', return_value=response):
            self.assertFalse(desktop.is_studio_service())

    def test_native_dialogs_and_drop_share_input_planner(self):
        import webview
        bridge = desktop.DesktopBridge()
        bridge._window = Mock()
        bridge._window.create_file_dialog.return_value = (str(self.product),)
        plan = bridge.choose_folder()
        self.assertEqual(plan['product']['product_dir'], str(self.product))
        bridge._window.create_file_dialog.assert_called_with(webview.FileDialog.FOLDER)
        nested = self.write('模型/日期/选择.max')
        bridge._window.create_file_dialog.return_value = (str(nested),)
        plan = bridge.choose_model()
        self.assertEqual(plan['product']['selected_model'], str(nested))
        self.assertEqual(plan['product']['product_dir'], str(self.product))
        bridge._window.create_file_dialog.assert_called_with(webview.FileDialog.OPEN,allow_multiple=False,file_types=('Max 模型 (*.max)',))
        bridge._drop({'dataTransfer':{'files':[{'pywebviewFullPath':str(nested)}]}})
        code = bridge._window.evaluate_js.call_args.args[0]
        drop_plan = json.loads(code[len('window.whitebgDesktopDrop('):-1])
        self.assertEqual(drop_plan,plan)
        bridge._drop({'dataTransfer':{'files':[{'name':'没有路径.max'}]}})
        code = bridge._window.evaluate_js.call_args.args[0]
        self.assertEqual(json.loads(code[len('window.whitebgDesktopDrop('):-1])['missing'],1)
        bridge._window.create_file_dialog.return_value = None
        self.assertIsNone(bridge.choose_folder())
        self.assertIsNone(bridge.choose_model())

    def test_system_close_lets_page_save_first(self):
        import threading
        bridge = desktop.DesktopBridge()
        window = bridge._window = Mock()
        window.events.closed = threading.Event()
        window.destroy.side_effect = window.events.closed.set
        # 网页收到通知后自己存好再调 window_close
        window.evaluate_js.side_effect = lambda script: (bridge.window_close(), True)[1]
        self.assertFalse(bridge._closing())                        # 第一次：先不关
        self.assertTrue(window.events.closed.wait(2))
        self.assertIn('__whitebgCloseSoon', window.evaluate_js.call_args.args[0])
        self.assertEqual(window.destroy.call_count, 1)
        self.assertTrue(bridge._closing())                         # 网页关窗口时再进来：放行
        # 网页出错：直接关
        bridge = desktop.DesktopBridge()
        window = bridge._window = Mock()
        window.events.closed = threading.Event()
        window.destroy.side_effect = window.events.closed.set
        window.evaluate_js.side_effect = RuntimeError('页面没了')
        self.assertFalse(bridge._closing())
        self.assertTrue(window.events.closed.wait(2))
        # 网页右上角的关闭按钮：直接关，不再拦
        bridge = desktop.DesktopBridge()
        bridge._window = Mock()
        bridge.window_close()
        self.assertTrue(bridge._closing())


if __name__=='__main__':unittest.main()
