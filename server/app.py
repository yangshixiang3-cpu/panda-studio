"""运行：python -m server.app；固定监听 127.0.0.1:8770。"""
import argparse
import io
import json
import mimetypes
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .ai_client import OpenAIClient, test_connection
from .ai_settings import AIError
from .calibration import validate_schema
from .cleanup import ExchangeCleanup
from .settings import MachineSettings, detect_max
from .lighting import MAX_LIGHTING_BYTES
from .core import (ROOT, MAX_REFERENCE_BYTES, Studio, StudioError,
                   _fallback_update_task_materials, _resolve_materials, inside)
from .textures import TaskTextures
from .remote_client import RemoteError
from . import node_setup
from .canvas import CanvasError, CanvasStore
from .image_gen import ImageGenerator

try:
    from . import materials as _materials
except ImportError:
    _materials = None

mimetypes.add_type('text/javascript', '.js')
mimetypes.add_type('text/javascript', '.mjs')
mimetypes.add_type('image/webp', '.webp')


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, handler):
        self._startup_thread = None
        super().__init__(address, handler)
        studio = getattr(handler, 'studio_instance', None)
        if studio is not None:
            studio.job_index.start()
            handler.exchange_cleanup.start()
            studio.remote.start()
            if getattr(studio, '_defer_startup', False):
                self._startup_thread = threading.Thread(
                    target=studio.run_startup_tasks, daemon=True, name='whitebg-startup')
                self._startup_thread.start()

    def server_close(self):
        studio = getattr(self.RequestHandlerClass, 'studio_instance', None)
        if studio is not None:
            self.RequestHandlerClass.exchange_cleanup.close()
            studio.remote.close()
            studio.job_index.close()
            studio.calibration.close()
        if self._startup_thread is not None:
            self._startup_thread.join(timeout=2)
        super().server_close()

    def server_bind(self):
        if os.name == 'nt':
            import socket
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def handler_for(studio):
    cleanup = ExchangeCleanup(studio)
    machine = MachineSettings(studio)
    textures = TaskTextures(studio)
    canvas_holder = [None]
    canvas_lock = threading.Lock()

    def get_canvas():
        if canvas_holder[0] is None:
            with canvas_lock:
                if canvas_holder[0] is None:
                    canvas_holder[0] = CanvasStore(studio)
        return canvas_holder[0]

    def _app_startup():
        get_canvas()
        cleanup.archive_jobs()

    if getattr(studio, '_defer_startup', False) and hasattr(studio, 'register_startup_hook'):
        studio.register_startup_hook(_app_startup)
    else:
        get_canvas()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'WhiteBG/0.1'
        timeout = 30
        studio_instance = studio
        exchange_cleanup = cleanup

        def log_message(self, fmt, *args):
            if sys.stdout is not None:
                print(f'本地请求：{self.address_string()} {fmt % args}', flush=True)

        def check_origin(self):
            if self.headers.get('Origin') not in (None,'http://127.0.0.1:8770',f'http://127.0.0.1:{self.server.server_port}'):
                raise StudioError('禁止来自外部网页的写入。',403)

        def reply(self, data, status=200):
            content=json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
            return self.reply_bytes(content, 'application/json; charset=utf-8', status)

        def reply_bytes(self, content, content_type, status=200):
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(content)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(content)

        def upload_annotation(self):
            self.check_origin()
            length = int(self.headers.get('Content-Length', '0'))
            metadata_limit = 65536
            if not 0 < length <= MAX_REFERENCE_BYTES+metadata_limit:
                raise StudioError('单张图片不能超过 50 MB。', 413)
            line = self.rfile.readline(min(length, metadata_limit))
            if not line.endswith(b'\n') or len(line) >= length:
                raise StudioError('标注上传格式无效。')
            metadata = json.loads(line)
            if not isinstance(metadata, dict):
                raise StudioError('标注上传格式无效。')
            content = self.rfile.read(length-len(line))
            if len(content) != length-len(line):
                raise StudioError('图片上传不完整。')
            if not 0 < len(content) <= MAX_REFERENCE_BYTES:
                raise StudioError('单张图片必须大于 0 且不超过 50 MB。', 413)
            source = metadata.get('source')
            if not isinstance(source, str) or not source.strip() or len(source) > 4000:
                raise StudioError('图片来源无效。')
            studio.refs.validate_regions(metadata.get('regions'), None)
            from PIL import Image
            try:
                with Image.open(io.BytesIO(content)) as raw:
                    if raw.format not in ('JPEG', 'PNG', 'WEBP'):
                        raise ValueError('仅支持 JPG、PNG、WebP。')
                    raw.verify()
            except (OSError, ValueError, Image.DecompressionBombError) as error:
                raise StudioError('不能读取这张图片。') from error
            with studio.lock:
                entry = studio.refs.save_annotation(metadata.get('regions'), source=source, content=content)
            return self.reply({'entry':entry}, 201)

        def body(self, limit=2_000_000):
            self.check_origin()
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                raise StudioError('请求必须使用 JSON。',415)
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=limit:raise StudioError('请求内容大小无效。')
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise StudioError('请求内容不完整。')
            value=json.loads(raw)
            if not isinstance(value,dict):raise StudioError('请求内容必须是 JSON 对象。')
            return value

        def serve_file(self, path, cache='no-cache'):
            self.send_response(200)
            self.send_header('Content-Type',mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
            self.send_header('Content-Length',str(path.stat().st_size))
            self.send_header('Cache-Control',cache)
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            if self.command!='HEAD':
                with path.open('rb') as stream:
                    import shutil
                    shutil.copyfileobj(stream,self.wfile)

        def ai(self, path, method):
            settings = studio.calibration.ai_settings

            def reply_settings(data):
                return self.reply({**data, 'codex_model': studio.config.get('codex_model', 'gpt-6.1-sol')})

            try:
                if path == '/api/ai/settings' and method == 'GET':
                    return reply_settings(settings.public())
                if method == 'POST':
                    data = self.body()
                    if path == '/api/ai/settings':
                        return reply_settings(settings.save(data))
                    if path == '/api/ai/models':
                        return self.reply({'models': OpenAIClient(settings, data).models()})
                    if path == '/api/ai/test':
                        if data.get('engine') == 'codex':
                            from .calibration import test_codex
                            return self.reply(test_codex(studio.config, studio.root))
                        return self.reply(test_connection(settings, validate_schema, data))
            except AIError as error:
                raise StudioError(str(error)) from None
            raise StudioError('接口不存在。', 404)

        def canvas_routes(self, path, method):
            """画布：项目和多张画布、图片、生图任务、记住的设置、生图接口设置。生图在后台线程，不拿 studio.lock。"""
            from .core import MAX_REFERENCE_BYTES
            canvas = get_canvas()
            parts=path.strip('/').split('/')[2:]          # 去掉 api/canvas
            try:
                if method in ('POST','PUT','DELETE'):self.check_origin()
                if parts==['projects']:
                    if method=='GET':return self.reply(canvas.list_projects())
                    if method=='POST':return self.reply(canvas.create_project(self.body().get('name')),201)
                if len(parts)>=2 and parts[0]=='projects':
                    pid=parts[1]
                    if len(parts)==2:
                        if method=='PUT':return self.reply(canvas.rename_project(pid,self.body().get('name')))
                        if method=='DELETE':return self.reply(canvas.delete_project(pid))
                    if parts[2:]==['restore'] and method=='POST':return self.reply(canvas.restore_project(pid))
                    if parts[2:]==['canvases'] and method=='POST':return self.reply(canvas.add_canvas(pid,self.body().get('name')),201)
                    if len(parts)>=4 and parts[2]=='canvases':
                        cid=parts[3]
                        if len(parts)==4 and method=='PUT':return self.reply(canvas.rename_canvas(pid,cid,self.body().get('name')))
                        if len(parts)==4 and method=='DELETE':return self.reply(canvas.delete_canvas(pid,cid))
                        if parts[4:]==['board']:
                            if method=='GET':return self.reply(canvas.board(pid,cid))
                            if method=='PUT':return self.reply(canvas.save_board(pid,cid,self.body(limit=6_000_000)))
                    if parts[2:]==['files'] and method=='POST':
                        length=int(self.headers.get('Content-Length','0'))
                        if not 0<length<=MAX_REFERENCE_BYTES:raise StudioError('单张图片不能超过 50 MB。',413)
                        content=self.rfile.read(length)
                        if len(content)!=length:raise StudioError('图片上传不完整。')
                        return self.reply(canvas.add_bytes(pid,content),201)
                    if parts[2:]==['files','copy'] and method=='POST':
                        data=self.body()
                        return self.reply(canvas.copy_files(pid,data.get('from'),data.get('files')))
                    if parts[2:]==['files','paths'] and method=='POST':
                        return self.reply({'files':canvas.add_paths(pid,self.body().get('paths'))},201)
                    if len(parts)==4 and parts[2]=='files' and method in ('GET','HEAD'):
                        width=parse_qs(urlsplit(self.path).query).get('w',[''])[0]
                        if width:   # 画布里的小图：缩略图。图片文件名是随机生成的、内容不会变，可以让浏览器缓存
                            return self.serve_file(canvas.thumb_path(pid,parts[3],width),'private, max-age=604800')
                        return self.serve_file(canvas.file_path(pid,parts[3]),'private, max-age=604800')
                if parts==['reveal'] and method=='POST':
                    data=self.body()
                    return self.reply(canvas.reveal(data.get('project') or None,data.get('file') or None))
                if parts==['generate'] and method=='POST':return self.reply(canvas.generate(self.body()),201)
                if len(parts)==2 and parts[0]=='runs' and method=='GET':return self.reply(canvas.run(parts[1]))
                if parts==['prefs']:
                    if method=='GET':return self.reply(canvas.prefs())
                    if method=='POST':return self.reply(canvas.save_prefs(self.body()))
                if parts==['settings'] and method=='GET':return self.reply(canvas.settings.public())
                if parts==['settings'] and method=='POST':return self.reply(canvas.settings.save(self.body()))
                # 生图渠道：删除、设为当前、复制
                if len(parts)==3 and parts[:2]==['settings','channels'] and method=='DELETE':
                    return self.reply(canvas.settings.delete(parts[2]))
                if len(parts)==4 and parts[:2]==['settings','channels'] and parts[3]=='activate' and method=='POST':
                    return self.reply(canvas.settings.activate(parts[2]))
                if len(parts)==4 and parts[:2]==['settings','channels'] and parts[3]=='copy' and method=='POST':
                    return self.reply(canvas.settings.copy(parts[2]),201)
                if parts==['models'] and method=='POST':
                    return self.reply(ImageGenerator(canvas.settings).models(self.body()))
            except CanvasError as error:
                raise StudioError(str(error),error.status) from None
            except AIError as error:
                raise StudioError(str(error)) from None
            raise StudioError('接口不存在。',404)

        def render_routes(self, path, method):
            """内网渲染机：本机选渲染位置（render-target）、把这台电脑设为渲染机（render-node）。
            会等网络或 Windows 管理员确认，不拿 studio.lock。"""
            remote = studio.remote
            try:
                if path=='/api/render-target/scan' and method=='POST':
                    self.check_origin()
                    return self.reply(remote.choices())
                if path=='/api/render-target/select' and method=='POST':
                    data=self.body()
                    target=None if data.get('mode')=='local' else {'host':data.get('host'),'port':data.get('port')}
                    remote.select(target)
                    return self.reply({'target':remote.target(),'runner':studio.runner_status()})
                if path=='/api/render-target/pair' and method=='POST':
                    data=self.body()
                    node=remote.pair(str(data.get('host') or ''),int(data.get('port') or 0),data.get('code'))
                    remote.select(node)
                    return self.reply({'target':remote.target(),'runner':studio.runner_status()})
                if path=='/api/render-target/add' and method=='POST':
                    return self.reply(remote.add(self.body().get('address')))
                if path=='/api/render-target/forget' and method=='POST':
                    data=self.body()
                    remote.settings.forget(str(data.get('host') or ''),int(data.get('port') or 0))
                    return self.reply(remote.choices(scan=False))
                if path=='/api/render-node' and method=='GET':
                    return self.reply(node_setup.status(studio))
                if path.startswith('/api/render-node/') and method=='POST':
                    self.check_origin()
                    action=path.rsplit('/',1)[1]
                    handlers={'enable':node_setup.enable,'disable':node_setup.disable,'code':node_setup.regenerate_code,
                              'forget-clients':node_setup.forget_clients,'firewall':node_setup.retry_firewall}
                    if action in handlers:
                        return self.reply(handlers[action](studio))
            except RemoteError as error:
                raise StudioError(str(error), error.status if error.status in (403,404,409,429) else
                                  409 if error.status==401 else 400 if error.status else 502) from None
            raise StudioError('接口不存在。',404)

        def dispatch(self):
            if self.headers.get('Host') not in ('127.0.0.1:8770',f'127.0.0.1:{self.server.server_port}'):
                raise StudioError('只接受本机地址访问。',403)
            url=urlsplit(self.path); path=unquote(url.path); query=parse_qs(url.query)
            arg=lambda k:query.get(k,[''])[0]
            method=self.command
            if path.startswith('/api/ai/'):
                return self.ai(path, method)
            if path=='/api/file' and method in ('GET','HEAD'):
                target=studio.allowed_file(arg('path'),arg('task_id'))
                width=arg('w')
                if width:target=studio.thumb_file(target,width)
                return self.serve_file(target)
            if not path.startswith('/api/') and method in ('GET','HEAD'):
                target=(studio.root/'web'/('index.html' if path=='/' else path.lstrip('/'))).resolve()
                if not inside(target,studio.root/'web'):raise StudioError('禁止读取这个路径。',403)
                return self.serve_file(studio.allowed_file(target))
            if path=='/api/settings' and method=='GET':
                return self.reply(machine.read())
            if path=='/api/settings' and method=='POST':
                data = self.body()
                with studio.lock:
                    return self.reply(machine.save(data))
            if path=='/api/settings/detect-max' and method=='POST':
                self.check_origin()
                return self.reply({'candidates':detect_max()})
            if path=='/api/settings/open-data' and method=='POST':
                self.check_origin()
                return self.reply(machine.open_data_dir())
            if path=='/api/settings/connect-max' and method=='POST':
                self.check_origin()
                with studio.lock:
                    return self.reply(machine.connect_max())
            if path=='/api/settings/remove-plugin' and method=='POST':
                self.check_origin()
                with studio.lock:
                    return self.reply(machine.remove_plugin())
            if path.startswith('/api/render-target') or path.startswith('/api/render-node'):
                return self.render_routes(path, method)
            if path=='/api/canvas' or path.startswith('/api/canvas/'):
                return self.canvas_routes(path, method)
            if path=='/api/storage' and method=='GET':
                return self.reply(cleanup.storage())
            if path=='/api/storage/cleanup' and method=='POST':
                if self.body().get('mode')!='delivered':
                    raise StudioError('清理模式无效。')
                return self.reply(cleanup.clean())
            if path=='/api/refs/annotated/upload' and method=='POST':
                return self.upload_annotation()
            if path=='/api/refs/annotated/source' and method=='POST':
                source = self.body().get('source')
                content = studio.refs.annotation_source(source)
                return self.reply_bytes(content, mimetypes.guess_type(source)[0])
            segments=path.strip('/').split('/')
            # 删除任务（移到回收站，可撤销）与撤销删除
            if method=='DELETE' and len(segments)==3 and segments[:2]==['api','tasks']:
                self.check_origin()
                with studio.lock:return self.reply(studio.delete_task(segments[2]))
            if method=='POST' and len(segments)==4 and segments[:2]==['api','tasks'] and segments[3]=='restore':
                self.check_origin()
                with studio.lock:return self.reply(studio.restore_task(segments[2]))
            if method=='POST' and len(segments)==4 and segments[:2]==['api','tasks'] and segments[3]=='variants':
                # 20 × 2000 个 255 字节点名按 JSON Unicode 转义编码也可完整提交。
                return self.reply(studio.set_variants(segments[2], self.body(limit=125_000_000)))
            if method=='GET' and len(segments)==4 and segments[:2]==['api','tasks'] and segments[3]=='texture':
                return self.serve_file(textures.find(segments[2], arg('name')))
            if len(segments)==5 and segments[:2]==['api','tasks'] and segments[3]=='calibration-refs':
                task_id, ref_id = segments[2], segments[4]
                self.check_origin()
                if method=='POST' and ref_id=='upload':
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=MAX_REFERENCE_BYTES:
                        raise StudioError('单张图片必须大于 0 且不超过 50 MB。',413)
                    content=self.rfile.read(length)
                    if len(content)!=length:raise StudioError('图片上传不完整。')
                    return self.reply({'entry':studio.calibration.upload_ref(task_id,arg('name'),content,arg('from') or None)},201)
                if method=='PUT':
                    return self.reply(studio.calibration.change_ref(task_id,ref_id,self.body()))
                if method=='DELETE':
                    return self.reply(studio.calibration.change_ref(task_id,ref_id))
                raise StudioError('接口不存在。',404)
            # 只读查询在锁外直接响应，不被慢速 NAS 扫描、缩图或入库阻塞
            if method=='GET':
                if path=='/api/health':
                    return self.reply({'service':'whitebg-studio','api_version':1,'root':str(studio.root),
                                       'version':next((p.read_text(encoding='utf-8').strip() for p in (studio.root/'VERSION',ROOT/'VERSION') if p.is_file()),'')})
                if path=='/api/runner':
                    return self.reply(studio.runner_status())
                if path=='/api/folders':
                    return self.reply(studio.browse(arg('path')))
                if path=='/api/tasks':
                    return self.reply({'tasks':cleanup.list_tasks(arg('product_dir'))})
                if path=='/api/refs':
                    return self.reply(studio.refs.read())
                if len(segments)==3 and segments[:2]==['api','tasks']:
                    return self.reply(cleanup.describe(segments[2]))
            # 二进制流上传与图片缩图在锁外读取/处理，仅在更新 JSON 记录时短暂加锁
            if path=='/api/refs/upload' and method=='POST':
                self.check_origin()
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=MAX_REFERENCE_BYTES:raise StudioError('单张参考图不能超过 50 MB。',413)
                content=self.rfile.read(length)
                if len(content)!=length:raise StudioError('图片上传不完整。')
                key, name = arg('key'), arg('name')
                scratch = studio.refs.read()
                added = studio.refs.add(key, name, content, scratch)
                with studio.lock:
                    library = studio.refs.read()
                    group = studio.refs.group(library, key)
                    if not any(e.get('file') == added.get('file') or e.get('source') == added.get('source') for e in group):
                        group.append(added)
                    studio.refs.save(library)
                return self.reply(library,201)
            if path.startswith('/api/tasks/') and path.endswith('/references/upload') and method=='POST':
                self.check_origin()
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=MAX_REFERENCE_BYTES:
                    raise StudioError('单张参考图必须大于 0 且不超过 50 MB。',413)
                content=self.rfile.read(length)
                if len(content)!=length:raise StudioError('图片上传不完整。')
                return self.reply({'task':studio.add_reference(path.split('/')[3],arg('name'),content)},201)
            if path.startswith('/api/tasks/') and path.endswith('/lighting-images') and method=='POST':
                self.check_origin()
                if self.headers.get('Content-Type','').split(';')[0]!='image/png':raise StudioError('光向示意图只接受 PNG。',415)
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=MAX_LIGHTING_BYTES:raise StudioError('光向示意图不能超过 20 MB。',413)
                content=self.rfile.read(length)
                if len(content)!=length:raise StudioError('图片上传不完整。')
                return self.reply(studio.add_lighting_image(path.split('/')[3],arg('no'),content),201)
            if path=='/api/runner/start' and method=='POST':
                self.check_origin()
                with studio.lock:
                    if studio.remote.target() is not None:
                        raise StudioError('现在用的是内网渲染机：那台电脑上的 Max 要在那边打开。想用本机 Max，先把渲染位置换回「本机 Max」。',409)
                    return self.reply(studio.runner.start())
            # 所有 JSON 请求先在 studio.lock 锁外读完请求体！
            data=self.body() if method in ('POST','PUT') else {}
            if path=='/api/refs/materials' and method=='POST':
                with studio.lock:
                    return self.reply(studio.refs.change_material(data.get('name'), data.get('keywords'),
                                                                  create=data.get('create') is True))
            if path=='/api/refs/annotated' and method=='POST':
                with studio.lock:
                    if data.get('action')=='delete':
                        studio.refs.delete_annotation(data.get('file'))
                        return self.reply({'deleted':True})
                    if data.get('action')=='update':
                        return self.reply({'entry':studio.refs.save_annotation(data.get('regions'), file=data.get('file'))})
                raise StudioError('标注操作无效。')
            if path=='/api/refs/status' and method=='POST':
                with studio.lock:
                    return self.reply(studio.refs.change_status(data.get('key'),data.get('file'),data.get('status')))
            if path=='/api/refs/images' and method=='POST':
                paths=data.get('paths')
                if not isinstance(paths,list) or not 1<=len(paths)<=100:raise StudioError('请选择 1—100 张图片。')
                key = data.get('key')
                scratch = studio.refs.read()
                added_entries = [studio.refs.add(key, source, library=scratch) for source in paths]
                with studio.lock:
                    library = studio.refs.read()
                    group = studio.refs.group(library, key)
                    for entry in added_entries:
                        if not any(e.get('file') == entry.get('file') or e.get('source') == entry.get('source') for e in group):
                            group.append(entry)
                    studio.refs.save(library)
                return self.reply(library,201)
            if path=='/api/scan' and method=='POST':
                return self.reply(studio.scan(data.get('product_dir','')))
            if path=='/api/products/delivery-dir' and method=='POST':
                return self.reply(studio.set_delivery_dir(data.get('product_dir',''),data.get('delivery_dir','')))
            if path=='/api/tasks' and method=='POST':
                return self.reply({'task':studio.create(data)},201)
            if len(segments)>=3 and segments[:2]==['api','tasks']:
                task_id=segments[2]; action=segments[3] if len(segments)>3 else ''
                if not action and len(segments)==3:
                    if method=='PUT':
                        return self.reply({'task':studio.update(task_id,data)})
                if method=='PUT' and len(segments)==4 and action=='materials':
                    mat_mod = _materials or _resolve_materials()
                    if mat_mod is not None:
                        return self.reply(mat_mod.update_task_materials(studio, task_id, data))
                    return self.reply(_fallback_update_task_materials(studio, task_id, data))
                if method=='POST':
                    if action=='references':
                        return self.reply({'task':studio.add_reference_paths(task_id,data.get('paths'))},201)
                    if action=='draft':
                        with studio.lock:
                            return self.reply({'task':studio.draft(task_id,data)})
                    if action=='switch-variant' and len(segments)==4:
                        return self.reply(studio.switch_variant(task_id,data))
                    if action=='variant-name' and len(segments)==4:
                        return self.reply({'task':studio.rename_variant(task_id,data)})
                    if action=='preview':
                        with studio.lock:
                            return self.reply(studio.preview(task_id,data))
                    if action=='jobs':
                        return self.reply({'job':studio.submit(task_id,data)},201)
                    if action=='calibration':
                        with studio.lock:
                            return self.reply({'task':studio.calibration.action(task_id,data)})
                    if action=='delivery-plan':
                        with studio.lock:
                            return self.reply(studio.delivery_plan(task_id))
                    if action=='deliver':
                        return self.reply({'delivered':studio.deliver(task_id,data.get('token'))})
            raise StudioError('接口不存在。',404)

        def safe_dispatch(self):
            try:self.dispatch()
            except StudioError as error:self.reply({'error':str(error)},error.status)
            except (FileNotFoundError,NotADirectoryError):self.reply({'error':'文件或文件夹不存在。'},404)
            except PermissionError:self.reply({'error':'没有权限访问这个文件或文件夹。'},403)
            except (json.JSONDecodeError,ValueError,KeyError,TypeError) as error:self.reply({'error':'请求或任务文件格式无效：'+str(error)},400)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,TimeoutError):pass
            except Exception as error:
                if sys.stderr is not None:print('服务错误：'+str(error),file=sys.stderr,flush=True)
                self.reply({'error':'操作失败：'+str(error)},500)

        do_GET=safe_dispatch
        do_HEAD=safe_dispatch
        do_POST=safe_dispatch
        do_PUT=safe_dispatch
        do_DELETE=safe_dispatch
    return Handler


def main():
    parser=argparse.ArgumentParser(description='白底工作台本地服务')
    parser.add_argument('--open',action='store_true',help='启动成功后用默认浏览器打开首页')
    args=parser.parse_args()
    studio=Studio(ROOT, defer_startup=True)
    try:server=LocalServer(('127.0.0.1',8770),handler_for(studio))
    except OSError:
        print('无法启动：本机端口 8770 已被占用或不能使用。请关闭已启动的白底工具或占用程序后重试。',file=sys.stderr)
        return 1
    print('白底工作台已启动：http://127.0.0.1:8770/\n关闭此窗口或按 Ctrl+C 停止服务。',flush=True)
    if args.open:webbrowser.open('http://127.0.0.1:8770/')
    try:server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:print('\n白底工作台已停止。')
    finally:
        studio.calibration.close()
        server.server_close()
    return 0


if __name__=='__main__':sys.exit(main())
