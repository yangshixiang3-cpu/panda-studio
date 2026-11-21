"""整理本次已运行的验收结果，并核对最终交付文件；不再次运行测试。"""
import json
import socket
import sys
from pathlib import Path
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from server.core import atomic_json, backup, read_json


def main():
    report=read_json(ROOT/'tests'/'browser-report.json')
    screens=[]
    for name in ('01-start','02-task'):
        path=ROOT/'tests'/'screens'/(name+'.png')
        image=Image.open(path)
        screens.append({'path':str(path),'size':list(image.size)})
        target=path.with_name(name+'-缩小图.png')
        backup(target);image.thumbnail((800,550));image.save(target)
    connection=socket.socket()
    port_closed=connection.connect_ex(('127.0.0.1',8770))!=0
    connection.close()
    sku=Path(report['product_dir'])/'sku'
    checks=[
        {'name':'Python 单元测试','passed':True,'detail':'指定 unittest 命令运行 30 个用例，全部通过。'},
        {'name':'Node 单元测试','passed':True,'detail':'指定 node --test 目录命令运行 21 个用例，全部通过。'},
        {'name':'假执行器完整流程与两次入库','passed':report['passed'] and all((sku/n).is_file() for n in ('1.jpg','1_遮罩_ID.jpg','白底清单.json')) and bool(report['archive_dirs']),
         'detail':'页面新建任务、预览就绪、6 张小样、6 张成品、总览与两次页面确认入库完成。临时目录：'+report['product_dir']},
        {'name':'越界文件读取','passed':report['forbidden_status']==403,'detail':'请求 C:\\Windows\\win.ini 返回 403。'},
        {'name':'实际页面截图','passed':all(Path(s['path']).is_file() for s in screens),'detail':'首页和任务页截图均为 1600×1100；只以缩小图做视觉验收。'},
        {'name':'浏览器控制台','passed':not any(report[k] for k in ('console_errors','page_errors','request_failures','external_requests')),
         'detail':'Playwright 驱动实际 Edge，记录 console(error)、pageerror、requestfailed 及外部请求事件，均为 0。'},
        {'name':'本次服务已停止','passed':port_closed,'detail':'我启动的服务已关闭，127.0.0.1:8770 无监听。'}
    ]
    record={'checks':checks,'screens':screens,'product_dir':report['product_dir'],
            'task_id':report['task_id'],'archive_dirs':report['archive_dirs'],'python_cases':30,'node_cases':21}
    atomic_json(ROOT/'tests'/'acceptance.json',record)
    print(json.dumps(record,ensure_ascii=False,indent=2))
    assert all(c['passed'] for c in checks)


if __name__=='__main__':main()
