"""开发专用假执行器，不调用 Max，也不代表最终渲染效果。"""
import argparse
import shutil
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server.core import ROOT, atomic_json, backup, inside, now, read_json
from max.wb_core import RunnerHeartbeat


def process_job(job_path, fixture, heartbeat=None):
    from PIL import Image, ImageDraw, ImageFont
    job=read_json(job_path); exchange=job_path.parent.parent
    status_path=job_path.with_name(job['job_id']+'.status.json')
    if status_path.exists():
        previous=read_json(status_path)
        # 已结束的、或已交给内网渲染机的作业（状态里带 remote）不处理
        if previous['state'] in ('done','failed') or previous.get('remote'):return False
    status={'job_id':job['job_id'],'state':'running','started_at':now(),'updated_at':now(),
            'items':[],'message':'开发占位，非正式渲染。','log':['假执行器开始处理。'],
            'runner_version':'fake'}
    def save():
        status['updated_at']=now();status['log']=status['log'][-200:];atomic_json(status_path,status)
    def output(path, preserve=True):
        target=Path(path)
        if not target.is_absolute() or not inside(target,exchange/'tasks'/job['task_id']):
            raise ValueError('任务输出路径越界。')
        target.parent.mkdir(parents=True,exist_ok=True)
        if preserve:backup(target)
        return target
    if heartbeat is not None:heartbeat.set_job(job['job_id'])
    try:
        save()
        if job['type']=='export_preview':
            shutil.copy2(fixture,output(job['out_fbx']))
            # 此测试 FBX 的实测闭合尺寸；网页读取 FBX 后按这些毫米尺寸核对。
            atomic_json(output(job['out_bbox'], preserve=False),{'units':'mm','max_world_bbox_mm':{'min':[-1000.003886291128,-800.000744123546,0],'max':[1000.003886291128,800.000744123546,2340.126965060298]},
                'size_mm':{'x':2000.007772582256,'y_up':2340.126965060298,'z_depth':1600.0014882470925},'mesh_count':180})
        elif job['type']=='render':
            font_path=Path('C:/Windows/Fonts/msyh.ttc')
            if job.get('want_manifest'):
                folder=Path(job['calibration_dir'])
                atomic_json(output(folder/'scene_manifest.json', preserve=False),{
                    'materials':[{'name':'摩卡红橡','class':'CoronaPhysicalMtl','objects':2,'kind':'wood','color':[180,140,105],'bitmap':'木纹.jpg','params':{}},
                                 {'name':'奶油白','class':'CoronaPhysicalMtl','objects':1,'kind':'solid','color':[230,225,210],'bitmap':None,'params':{}},
                                 {'name':'长虹玻璃','class':'CoronaPhysicalMtl','objects':1,'kind':'glass','color':[180,200,205],'bitmap':None,'params':{}}],
                    'lights':[{'name':'主灯','intensity':15,'pos_mm':[1000,-1000,2000]}],
                    'environment':{'reflect_map':None},'product_center_mm':[0,0,0],'unsupported':[]})
                atomic_json(output(folder/'regions'/'legend.json', preserve=False),{'#FF0000':'摩卡红橡','#00FF00':'奶油白','#0000FF':'长虹玻璃'})
            for item in job['items']:
                progress={'no':item['no'],'state':'rendering','started_at':now(),
                          'out_image':item['out_image'],'out_mask':item['out_mask'],'message':''}
                status['items'].append(progress);save()
                w,h=item['width'],item['height']; image=Image.new('RGB',(w,h),'#F0F0F0'); draw=ImageDraw.Draw(image)
                font=ImageFont.truetype(str(font_path),max(16,int(min(w,h)/25))) if font_path.exists() else ImageFont.load_default()
                cam=item['camera']; text=f"开发占位图 · {item['no']:02d}\n转动 {cam.get('azimuth_deg',0):.1f}° / 俯视 {cam.get('elevation_deg',0):.1f}°\n{w} × {h}\n仅用于流程测试"
                draw.rectangle((w*.1,h*.1,w*.9,h*.9),outline='#B8B2AA',width=max(1,w//300))
                draw.rectangle((w*.22,h*.32,w*.78,h*.7),fill='#CEBDA8',outline='#6A4630',width=max(2,w//200))
                draw.line((w*.5,h*.32,w*.5,h*.7),fill='#6A4630',width=max(2,w//300))
                if job.get('want_manifest'):
                    region=Image.new('RGB',(w,h),'black'); region_draw=ImageDraw.Draw(region)
                    blocks=[((.22,.32,.49,.7),'#FF0000','#B48C69'),((.5,.32,.78,.5),'#00FF00','#E6E1D2'),((.5,.51,.78,.7),'#0000FF','#B4C8CD')]
                    brightness=job.get('overrides') or {}
                    brightness=brightness.get('materials',{}).get('摩卡红橡',{}).get('brightness_mul',1)
                    for box,color,fill in blocks:
                        coords=(int(w*box[0]),int(h*box[1]),int(w*box[2]),int(h*box[3]))
                        region_draw.rectangle(coords,fill=color)
                        if color=='#FF0000':fill=tuple(min(255,int(v*brightness)) for v in (180,140,105))
                        draw.rectangle(coords,fill=fill)
                    region.save(output(Path(job['calibration_dir'])/'regions'/f"{item['no']}.png"),'PNG')
                draw.multiline_text((w*.12,h*.13),text,fill='#40382F',font=font,spacing=max(8,w//80))
                image.save(output(item['out_image']),'JPEG',quality=90)
                if item['out_mask']:
                    mask=Image.new('RGB',(w,h),'black');ImageDraw.Draw(mask).rectangle((w*.22,h*.32,w*.78,h*.7),fill='white')
                    mask.save(output(item['out_mask']),'JPEG',quality=95)
                progress['state']='done';save()
        else:raise ValueError('不支持的任务类型。')
        status['state']='done';status['log'].append('假执行器处理完成。')
    except Exception as error:
        status.update(state='failed',message=str(error));status['log'].append(str(error))
        for item in status['items']:
            if item['state']!='done':item.update(state='failed',message=str(error))
    try:
        save()
    finally:
        if heartbeat is not None:heartbeat.set_job(None)
    print('假执行器：'+job['job_id']+' '+status['state'],flush=True)
    return True


def run_once(exchange,fixture,heartbeat=None):
    jobs=[]
    for path in (Path(exchange)/'jobs').glob('*.json'):
        if '.status.' not in path.name:
            job=read_json(path);jobs.append((job['created_at'],path.name,path))
    return sum(process_job(p,fixture,heartbeat) for _,_,p in sorted(jobs))


def interrupt(signum, frame):
    raise KeyboardInterrupt


def main():
    parser=argparse.ArgumentParser(description='白底工作台开发假执行器')
    parser.add_argument('--exchange',default=str(ROOT/'exchange'))
    parser.add_argument('--fixture',default=str(ROOT/'tests'/'fixtures'/'L型咖啡柜_绑定.fbx'))
    parser.add_argument('--once',action='store_true',help='处理现有任务后退出')
    args=parser.parse_args();exchange=Path(args.exchange).resolve()
    if not inside(exchange,ROOT):parser.error('开发交换目录必须位于 whitebg-studio 内。')
    signal.signal(signal.SIGTERM, interrupt)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, interrupt)
    heartbeat = RunnerHeartbeat(exchange, 'fake', lambda message: print(message, file=sys.stderr))
    heartbeat.start()
    print('开发假执行器已启动；不会调用 Max。',flush=True)
    try:
        while True:
            run_once(exchange,Path(args.fixture),heartbeat)
            if args.once:return
            time.sleep(0.5)
    except KeyboardInterrupt:print('假执行器已停止。')
    finally:heartbeat.stop()


if __name__=='__main__':main()
