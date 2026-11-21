"""运行：python -m server.app；固定监听 127.0.0.1:8770。"""
import argparse
import json
import mimetypes
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .ai_client import OpenAIClient, test_connection
from .ai_settings import AIError
from .calibration import validate_schema
from .cleanup import ExchangeCleanup
from .settings import MachineSettings, detect_max
from .lighting import MAX_LIGHTING_BYTES
from .core import ROOT, MAX_REFERENCE_BYTES, Studio, StudioError, inside
from .textures import TaskTextures

mimetypes.add_type('text/javascript', '.js')
mimetypes.add_type('text/javascript', '.mjs')


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, handler):
        super().__init__(address, handler)
        studio = getattr(handler, 'studio_instance', None)
        if studio is not None:
            studio.job_index.start()
            handler.exchange_cleanup.start()

    def server_close(self):
        studio=getattr(self.RequestHandlerClass,'studio_instance',None)
        if studio is not None:
            self.RequestHandlerClass.exchange_cleanup.close()
            studio.job_index.close()
            studio.calibration.close()
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

    class Handler(BaseHTTPRequestHandler):
        server_version = 'WhiteBG/0.1'
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
            with studio.lock:
                entry = studio.refs.save_annotation(metadata.get('regions'), source=metadata.get('source'), content=content)
            return self.reply({'entry':entry}, 201)

        def body(self, limit=2_000_000):
            self.check_origin()
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                raise StudioError('请求必须使用 JSON。',415)
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=limit:raise StudioError('请求内容大小无效。')
            value=json.loads(self.rfile.read(length))
            if not isinstance(value,dict):raise StudioError('请求内容必须是 JSON 对象。')
            return value

        def serve_file(self, path):
            self.send_response(200)
            self.send_header('Content-Type',mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
            self.send_header('Content-Length',str(path.stat().st_size))
            self.send_header('Cache-Control','no-cache')
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

        def dispatch(self):
            if self.headers.get('Host') not in ('127.0.0.1:8770',f'127.0.0.1:{self.server.server_port}'):
                raise StudioError('只接受本机地址访问。',403)
            url=urlsplit(self.path); path=unquote(url.path); query=parse_qs(url.query)
            arg=lambda k:query.get(k,[''])[0]
            method=self.command
            if path.startswith('/api/ai/'):
                return self.ai(path, method)
            if path=='/api/file' and method in ('GET','HEAD'):
                return self.serve_file(studio.allowed_file(arg('path'),arg('task_id')))
            if not path.startswith('/api/') and method in ('GET','HEAD'):
                target=(studio.root/'web'/('index.html' if path=='/' else path.lstrip('/'))).resolve()
                if not inside(target,studio.root/'web'):raise StudioError('禁止读取这个路径。',403)
                return self.serve_file(studio.allowed_file(target))
            if path=='/api/settings' and method=='GET':
                return self.reply(machine.read())
            if path=='/api/settings' and method=='POST':
                self.check_origin()
                with studio.lock:
                    return self.reply(machine.save(self.body()))
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
                    return self.reply({'entry':studio.calibration.upload_ref(task_id,arg('name'),content)},201)
                if method=='PUT':
                    return self.reply(studio.calibration.change_ref(task_id,ref_id,self.body()))
                if method=='DELETE':
                    return self.reply(studio.calibration.change_ref(task_id,ref_id))
                raise StudioError('接口不存在。',404)
            with studio.lock:
                if method=='GET':
                    if path=='/api/health':return self.reply({'service':'whitebg-studio','api_version':1,'root':str(studio.root)})
                    if path=='/api/runner':return self.reply(studio.runner.status())
                    if path=='/api/folders':return self.reply(studio.browse(arg('path')))
                    if path=='/api/tasks':return self.reply({'tasks':cleanup.list_tasks(arg('product_dir'))})
                    if path=='/api/refs':return self.reply(studio.refs.read())
                if path=='/api/refs/upload' and method=='POST':
                    self.check_origin()
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=MAX_REFERENCE_BYTES:raise StudioError('单张参考图不能超过 50 MB。',413)
                    content=self.rfile.read(length)
                    if len(content)!=length:raise StudioError('图片上传不完整。')
                    library=studio.refs.read()
                    studio.refs.add(arg('key'),arg('name'),content,library)
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
                    return self.reply(studio.runner.start())
                data=self.body() if method in ('POST','PUT') else {}
                if path=='/api/refs/materials' and method=='POST':
                    return self.reply(studio.refs.change_material(data.get('name'), data.get('keywords'),
                                                                  create=data.get('create') is True))
                if path=='/api/refs/annotated' and method=='POST':
                    if data.get('action')=='delete':
                        studio.refs.delete_annotation(data.get('file'))
                        return self.reply({'deleted':True})
                    if data.get('action')=='update':
                        return self.reply({'entry':studio.refs.save_annotation(data.get('regions'), file=data.get('file'))})
                    raise StudioError('标注操作无效。')
                if path=='/api/refs/status' and method=='POST':
                    return self.reply(studio.refs.change_status(data.get('key'),data.get('file'),data.get('status')))
                if path=='/api/refs/images' and method=='POST':
                    paths=data.get('paths')
                    if not isinstance(paths,list) or not 1<=len(paths)<=100:raise StudioError('请选择 1—100 张图片。')
                    library=studio.refs.read()
                    for source in paths:studio.refs.add(data.get('key'),source,library=library)
                    studio.refs.save(library)
                    return self.reply(library,201)
                if path=='/api/scan' and method=='POST':return self.reply(studio.scan(data.get('product_dir','')))
                if path=='/api/products/delivery-dir' and method=='POST':
                    return self.reply(studio.set_delivery_dir(data.get('product_dir',''),data.get('delivery_dir','')))
                if path=='/api/tasks' and method=='POST':return self.reply({'task':studio.create(data)},201)
                if len(segments)>=3 and segments[:2]==['api','tasks']:
                    task_id=segments[2]; action=segments[3] if len(segments)>3 else ''
                    if not action:
                        if method=='GET':return self.reply(cleanup.describe(task_id))
                        if method=='PUT':return self.reply({'task':studio.update(task_id,data)})
                    if method=='POST':
                        if action=='references':return self.reply({'task':studio.add_reference_paths(task_id,data.get('paths'))},201)
                        if action=='draft':return self.reply({'task':studio.draft(task_id,data)})
                        if action=='switch-variant' and len(segments)==4:
                            return self.reply(studio.switch_variant(task_id,data))
                        if action=='variant-name' and len(segments)==4:
                            return self.reply({'task':studio.rename_variant(task_id,data)})
                        if action=='preview':return self.reply(studio.preview(task_id,data))
                        if action=='jobs':return self.reply({'job':studio.submit(task_id,data)},201)
                        if action=='calibration':return self.reply({'task':studio.calibration.action(task_id,data)})
                        if action=='delivery-plan':return self.reply(studio.delivery_plan(task_id))
                        if action=='deliver':return self.reply({'delivered':studio.deliver(task_id,data.get('token'))})
                raise StudioError('接口不存在。',404)

        def safe_dispatch(self):
            try:self.dispatch()
            except StudioError as error:self.reply({'error':str(error)},error.status)
            except (FileNotFoundError,NotADirectoryError):self.reply({'error':'文件或文件夹不存在。'},404)
            except PermissionError:self.reply({'error':'没有权限访问这个文件或文件夹。'},403)
            except (json.JSONDecodeError,ValueError,KeyError,TypeError) as error:self.reply({'error':'请求或任务文件格式无效：'+str(error)},400)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
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
    studio=Studio(ROOT)
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
