// 浏览器与假执行器均在隔离环境运行，待删除副本位于 tests/tmp。
const assert=require('node:assert/strict');
const fs=require('node:fs');
const net=require('node:net');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {once}=require('node:events');
const {setTimeout:sleep}=require('node:timers/promises');
const workspace=path.resolve(__dirname,'..','..');
const {root:synced,env,withPage,modelReady,readyImages,until}=require(path.join(workspace,'codex-tasks','wb-10b-tmp','harness.cjs'));
const url='http://127.0.0.1:8796';
const shots=path.join(workspace,'codex-tasks','wb-16-shots');
const firstRound=process.argv.includes('--screens')?1:0;
const python=String.raw`
import shutil, sys, threading
from pathlib import Path
from PIL import Image, ImageOps
from max.wb_core import RunnerHeartbeat
from server.app import LocalServer, handler_for
from server.core import Studio, atomic_json, read_json
from tools.fake_runner import run_once

root = Path(sys.argv[1]).resolve()
config = read_json(Path('config.json'))
first_round = int(sys.argv[3])
config.update(exchange_dir='exchange', max_exe='', calibration_auto=False, calibration_max_rounds=0,
              codex_command=[sys.executable, '-B', sys.argv[2]],
              smallshot_size={'portrait':[300,400],'landscape':[400,300]})
studio = Studio(root, config)
studio.calibration.ai_settings.key_path = root/'test-key.bin'
product = root/'测试咖啡柜'
product.mkdir()
fixture = next(Path('tests/fixtures').glob('*.fbx'))
model = product/'开发占位.max'
model.touch()
with Image.open('tests/fixtures/侧面.png') as source:
    source.thumbnail((1100, 1100))
    source.save(root/'柜体材质.png')
    ImageOps.mirror(source).save(root/'门板材质.png')
task = studio.create({'product_dir':str(product), 'model_file':str(model)})

# 用预备小样提供场景清单，默认从第 0 轮验收，截图时用正常轮次编号。
job = studio._submit_render(task, 'smallshot', round_no=first_round-1)
preface = studio.calibration.record(first_round-1, job['job_id'], None)
preface.update(verdict='ok', summary='场景材质已读取。', confidence=1)
task['ai_calibration']['rounds'].append(preface)
studio.save(task)
run_once(studio.exchange, fixture)
atomic_json(root/'fixture.json', {'id':task['id']})

stop = threading.Event()
heartbeat = RunnerHeartbeat(studio.exchange, 'fake', print)
heartbeat.start()
def render():
    while not stop.is_set():
        run_once(studio.exchange, fixture, heartbeat)
        stop.wait(.15)

worker = threading.Thread(target=render, daemon=True)
worker.start()
try:
    with LocalServer(('127.0.0.1',8796), handler_for(studio)) as service:
        def shutdown():
            sys.stdin.readline()
            service.shutdown()
        threading.Thread(target=shutdown,daemon=True).start()
        service.serve_forever(.1)
finally:
    stop.set()
    worker.join(timeout=3)
    heartbeat.stop()
    studio.calibration.close()
`;

async function get(endpoint){
  const response=await fetch(url+endpoint);
  assert.ok(response.ok,`接口 ${endpoint} 返回 ${response.status}`);
  return response.json();
}

async function draw(page,group,material,start,end){
  await page.locator('#annotate-draw').click();
  await page.locator('#annotate-task-toggle').click();
  await page.getByRole('group',{name:group,exact:true}).getByRole('button',{name:material,exact:true}).click();
  const {x,y,width,height}=await page.locator('#annotate-frame').boundingBox();
  await page.mouse.move(x+start[0]*width,y+start[1]*height);
  await page.mouse.down();
  await page.mouse.move(x+end[0]*width,y+end[1]*height,{steps:8});
  await page.mouse.up();
}

async function flow(page,root){
  const {id}=JSON.parse(fs.readFileSync(path.join(root,'fixture.json'),'utf8'));
  const bundle=()=>get('/api/tasks/'+id);
  const refs=async()=>(await bundle()).task.ai_calibration.task_refs;
  const editor=page.locator('#annotate-dialog'),dialog=page.locator('#dialog');
  const cards=page.locator('[data-calibration-ref]');
  const problems=[];
  page.on('pageerror',error=>problems.push(error.message));
  page.on('response',response=>{if(response.status()>=400)problems.push(`${response.status()} ${response.url()}`);});
  await page.route('**/*',route=>{
    if(route.request().url().startsWith(url+'/'))return route.continue();
    problems.push('意外外部请求 '+route.request().url());
    return route.abort();
  });
  const shot=name=>page.screenshot({path:path.join(shots,name),animations:'disabled'});
  const complete=async n=>{
    await dialog.waitFor({state:'hidden'});
    await until(async()=>{
      const c=(await bundle()).task.ai_calibration;
      assert.notEqual(c.state,'failed',c.rounds.at(-1)?.error);
      return c.state==='waiting_user'&&c.rounds.some(r=>r.n===n&&r.verdict);
    },`第 ${n} 轮校准完成`);
    await page.click('#calibration-history');
  };
  const round=n=>dialog.locator('.calibration-round').filter({has:page.getByRole('heading',{name:`第 ${n} 轮`,exact:true})});
  await page.goto(url+'/#task/'+id);
  await modelReady(page);
  await page.click('#calibration-history');
  assert.equal(await cards.count(),0);
  await page.locator('#calibration-ref-input').setInputFiles(path.join(root,'柜体材质.png'));
  await editor.waitFor();
  await draw(page,'场景材质','摩卡红橡',[.34,.115],[.63,.205]);
  await draw(page,'参考库材质','长虹玻璃',[.708,.15],[.828,.69]);
  assert.equal(await page.locator('#annotate-in-library').isChecked(),false);
  await page.locator('#annotate-in-library').check();
  await page.locator('#annotate-save').click();
  await editor.waitFor({state:'hidden'});
  const [first]=await refs();
  assert.deepEqual(first.regions.map(r=>r.material_source),['scene','library']);
  assert.deepEqual((await get('/api/refs')).annotated[0].regions.map(r=>r.material),['长虹玻璃']);
  await page.click('#start-calibration');
  await complete(firstRound);
  await round(firstRound).getByText('用了 1 张本任务参考',{exact:true}).waitFor();
  assert.deepEqual((await bundle()).task.ai_calibration.rounds.find(r=>r.n===firstRound).task_refs_used,
    [{id:first.id,region_index:0},{id:first.id,region_index:1}]);

  const secondBytes=fs.readFileSync(path.join(root,'门板材质.png'));
  await dialog.evaluate((target,encoded)=>{
    const bytes=Uint8Array.from(atob(encoded),char=>char.charCodeAt(0));
    const transfer=new DataTransfer();
    transfer.items.add(new File([bytes],'门板材质.png',{type:'image/png'}));
    target.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer}));
  },secondBytes.toString('base64'));
  await editor.waitFor();
  await draw(page,'场景材质','摩卡红橡',[.40,.30],[.65,.41]);
  await page.locator('#annotate-save').click();
  await editor.waitFor({state:'hidden'});
  const [,second]=await refs();
  await dialog.locator('.annotation-outline').nth(2).waitFor();
  assert.equal(await cards.count(),2);
  await readyImages(page,'[data-calibration-ref] img');
  if(firstRound===1){
    await page.locator('#toast').waitFor({state:'hidden'});
    await shot('01-calibration-refs.png');
  }
  await cards.first().click();
  await editor.waitFor();
  await page.locator('#annotate-task-toggle').click();
  await page.getByRole('group',{name:'场景材质',exact:true}).waitFor();
  await page.getByRole('group',{name:'参考库材质',exact:true}).waitFor();
  if(firstRound===1)await shot('02-annotate-scene-materials.png');
  await page.locator('#annotate-cancel').click();
  await editor.waitFor({state:'hidden'});
  await page.click('#retry-calibration');
  await complete(firstRound+1);
  await round(firstRound+1).getByText('用了 2 张本任务参考',{exact:true}).waitFor();
  assert.deepEqual(new Set((await bundle()).task.ai_calibration.rounds.find(r=>r.n===firstRound+1).task_refs_used.map(ref=>ref.id)),
    new Set([first.id,second.id]));

  await page.locator(`[data-remove-ref="${first.id}"]`).click();
  const confirm=page.locator('#calibration-ref-delete');
  await confirm.getByRole('button',{name:'取消'}).click();
  assert.equal(await cards.count(),2);
  await page.locator(`[data-remove-ref="${first.id}"]`).click();
  await confirm.getByRole('button',{name:'确认删除'}).click();
  await cards.nth(1).waitFor({state:'detached'});
  assert.deepEqual((await refs()).map(entry=>entry.id),[second.id]);
  assert.equal((await get('/api/refs')).annotated.length,1);

  await page.evaluate(source=>window.whitebgDesktopDrop({images:[source]}),path.join(root,'柜体材质.png'));
  await editor.waitFor();
  await page.locator('#annotate-cancel').click();
  assert.equal((await refs()).length,2);
  assert.deepEqual(problems,[]);
  console.log(`文件选择、场景与参考库两框、入库筛选、第 ${firstRound} 轮与再调一轮、浏览器及桌面拖放和二次确认删除均通过。`);
}

async function run(){
  const connection=net.connect({host:'127.0.0.1',port:8796});
  const occupied=await new Promise(resolve=>{
    connection.once('error',()=>resolve(false));connection.once('connect',()=>{connection.destroy();resolve(true);});
  });
  assert.ok(!occupied,'隔离测试端口 8796 已占用。');
  const tmp=path.join(__dirname,'tmp');
  fs.mkdirSync(tmp,{recursive:true});
  fs.mkdirSync(shots,{recursive:true});
  const root=fs.mkdtempSync(path.join(tmp,'wb16-flow-'));
  fs.cpSync(path.join(synced,'web'),path.join(root,'web'),{recursive:true});
  const log=fs.openSync(path.join(root,'service.log'),'w');
  const service=spawn('python.exe',['-B','-u','-c',python,root,path.join(__dirname,'fake_codex.py'),String(firstRound)],
    {cwd:synced,env,stdio:['pipe',log,log],windowsHide:true});
  fs.closeSync(log);
  const stopped=once(service,'close');
  service.stdin.on('error',()=>{});
  try{
    await until(()=>get('/api/health').then(health=>path.resolve(health.root)===root,()=>false),'隔离服务');
    await withPage(page=>flow(page,root));
  }finally{
    service.stdin.end('停止\n');
    await Promise.race([stopped,sleep(3000)]);
    if(service.exitCode===null)service.kill();
    await stopped;
    console.log('隔离验收记录 '+root);
  }
}

run().catch(error=>{console.error(error);process.exitCode=1;});
