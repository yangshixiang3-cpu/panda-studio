// 使用同步后的代码在 8796 启动隔离服务，所有可清理的任务只放在 tests/tmp。
const assert=require('node:assert/strict');
const fs=require('node:fs');
const net=require('node:net');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {once}=require('node:events');
const {setTimeout:sleep}=require('node:timers/promises');
const {chromium}=require(process.env.PLAYWRIGHT_CORE||'playwright-core');

const tmp=path.join(__dirname,'tmp');
const synced=path.join(tmp,'e2e','studio');
const shots=path.resolve(process.argv[2]||path.join(tmp,'refs-cleanup-shots'));
const url='http://127.0.0.1:8796';
const python=String.raw`
import json, shutil, sys, threading
from pathlib import Path
from PIL import Image
from server.app import LocalServer, handler_for
from server.core import Studio, now

root = Path(sys.argv[1])
fixture = Path('tests/fixtures')
studio = Studio(root, {'exchange_dir': 'exchange', 'max_exe':'', 'calibration_auto':False,
                       'codex_command':[sys.executable, str(Path('tools/fake_codex.py').resolve())]})
studio.calibration.ai_settings.key_path = root/'test-key.bin'
source = root/'材质对照.png'
with Image.open(fixture/'侧面.png') as image:
    image.thumbnail((1500,1500))
    image.convert('RGB').save(source)
for name in ('咖啡柜 · 已入库', '餐边柜 · 待渲染'):
    product = root/'产品'/name
    product.mkdir(parents=True)
    task = studio.create({'product_dir':str(product)})
    folder = studio.task_dir(task['id'])
    preview = folder/'preview'/'preview.fbx'
    preview.parent.mkdir()
    shutil.copy2(next(fixture.glob('*.fbx')),preview)
    task.update(preview_source='file',preview_fbx=str(preview))
    smallshots = folder/'smallshots'
    smallshots.mkdir()
    with Image.open(source) as image:
        image.save(smallshots/'1.jpg')
    task['items'][0]['smallshot']['state']='done'
    if name.startswith('咖啡柜'):
        sku = product/'sku'
        sku.mkdir()
        shutil.copy2(smallshots/'1.jpg',sku/'1.jpg')
        task['delivered'] = {'at':now(),'sku_dir':str(sku),'files':['1.jpg']}
        archive = folder/'calibration'/'round-1'
        archive.mkdir(parents=True)
        with (archive/'fake-cache.bin').open('wb') as out:
            out.truncate(28*1024*1024)
        task['ai_calibration']['rounds']=[studio.calibration.record(1,'',None)]
        (root/'case.json').write_text(json.dumps({'delivered':task['id'],'sku':str(sku/'1.jpg')}),encoding='utf-8')
    studio.save(task)

with LocalServer(('127.0.0.1',8796),handler_for(studio)) as service:
    def stop():
        sys.stdin.readline()
        service.shutdown()
    threading.Thread(target=stop,daemon=True).start()
    service.serve_forever(.1)
`;

async function request(endpoint){
  const response=await fetch(url+endpoint);
  assert.ok(response.ok,`接口 ${endpoint} 返回 ${response.status}`);
  return response.json();
}
async function waitFor(test,label){
  for(let attempt=0;attempt<100;attempt++){
    if(await test())return;
    await sleep(100);
  }
  throw Error('等待超时：'+label);
}
async function drag(page,x,y,endX,endY){
  await page.mouse.move(x,y);
  await page.mouse.down();
  await page.mouse.move(endX,endY,{steps:8});
  await page.mouse.up();
}
async function draw(page,material,start,end){
  await page.locator('#annotate-draw').click();
  await page.locator('#annotate-material').selectOption(material);
  const {x,y,width,height}=await page.locator('#annotate-frame').boundingBox();
  await drag(page,x+start[0]*width,y+start[1]*height,x+end[0]*width,y+end[1]*height);
}
const crop=material=>`[data-ref-group="${material}"] [data-annotation-region]`;

async function browserFlow(page,root){
  const fixture=JSON.parse(fs.readFileSync(path.join(root,'case.json'),'utf8'));
  const source=path.join(root,'材质对照.png');
  const sourceBytes=fs.readFileSync(source);
  const editor=page.locator('#annotate-dialog');
  const select=page.locator('#annotate-material');
  const thumbnail=page.locator('#annotated-grid [data-annotation]');
  const dialog=page.locator('#dialog');
  const task=page.locator(`[data-task="${fixture.delivered}"]`);
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('response',response=>{if(response.status()>=400)errors.push(`${response.status()} ${response.url()}`);});
  const screenshot=name=>page.screenshot({path:path.join(shots,name),animations:'disabled'});

  await page.goto(url+'/#refs');
  await page.locator('[data-ref-group="摩卡红橡"]').waitFor();
  const picker=page.waitForEvent('filechooser');
  await page.locator('#annotate-add').click();
  await (await picker).setFiles(source);
  await editor.waitFor();
  await draw(page,'摩卡红橡',[.34,.115],[.63,.205]);
  await draw(page,'奶油白',[.708,.15],[.828,.69]);
  await page.locator('#annotate-save').click();
  await thumbnail.waitFor();
  await page.reload();
  await thumbnail.waitFor();
  assert.equal(await thumbnail.locator('.annotation-outline').count(),2);
  await page.locator(crop('摩卡红橡')).waitFor();
  await page.locator(crop('奶油白')).waitFor();
  await page.locator(crop('奶油白')).click();
  assert.equal(await select.inputValue(),'奶油白');
  assert.equal(await editor.locator('.annotate-region.selected').count(),1);
  await select.selectOption('长虹玻璃');
  await page.locator('#annotate-save').click();
  await page.locator(crop('长虹玻璃')).waitFor({state:'attached'});
  assert.equal(await page.locator(crop('奶油白')).count(),0);

  await thumbnail.click();
  await draw(page,'幻影灰',[.115,.25],[.222,.67]);
  const frame=await page.locator('#annotate-frame').boundingBox();
  await page.mouse.move(frame.x+frame.width/2,frame.y+frame.height/2);
  await page.mouse.wheel(0,-250);
  await page.waitForFunction(()=>document.querySelector('#annotate-zoom').textContent!=='100%');
  const zoomed=await page.locator('#annotate-frame').boundingBox();
  await page.locator('#annotate-pan').click();
  await drag(page,800,520,858,549);
  assert.ok((await page.locator('#annotate-frame').boundingBox()).y>zoomed.y+20);
  await page.locator('#annotate-zoom').click();
  await page.locator('#annotate-draw').click();
  const third=editor.locator('[data-region="2"]');
  const initial=await third.boundingBox();
  await drag(page,initial.x+initial.width/2,initial.y+initial.height/2,initial.x+initial.width/2+15,initial.y+initial.height/2-10);
  assert.ok((await third.boundingBox()).x>initial.x+8);
  const grip=await third.locator('[data-corner="se"]').boundingBox();
  await drag(page,grip.x+6,grip.y+6,grip.x+27,grip.y+14);
  assert.ok((await third.boundingBox()).width>initial.width+12);
  await page.locator('#annotate-save').click();
  await page.waitForFunction(()=>document.querySelectorAll('#annotated-grid .annotation-outline').length===3);
  await page.evaluate(()=>scrollTo(0,0));
  await Promise.all([thumbnail.locator('img'),page.locator(crop('摩卡红橡')).locator('img')].map(img=>img.evaluate(el=>el.decode())));
  await screenshot('01-refs-annotated.png');
  await page.locator(crop('摩卡红橡')).click();
  assert.equal(await editor.locator('.annotate-region').count(),3);
  assert.equal(await editor.locator('.selected .region-label').textContent(),'摩卡红橡');
  await screenshot('02-annotate-dialog.png');
  await page.keyboard.press('Delete');
  assert.equal(await editor.locator('.annotate-region').count(),2);
  await editor.locator('[data-region="0"]').click({position:{x:40,y:40}});
  await editor.getByRole('button',{name:'删除框'}).click();
  assert.equal(await editor.locator('.annotate-region').count(),1);
  await page.locator('#annotate-delete').click();
  await page.locator('#annotate-delete-cancel').click();
  assert.ok(await editor.isVisible());
  await page.locator('#annotate-delete').click();
  await page.locator('#annotate-delete-confirm').click();
  await thumbnail.waitFor({state:'detached'});
  assert.deepEqual((await request('/api/refs')).annotated,[]);
  assert.ok(fs.readFileSync(source).equals(sourceBytes));

  // 浏览器空白处拖放与桌面回调均进入未保存的标注编辑器。
  const image64=sourceBytes.toString('base64');
  await page.evaluate(data=>{
    const transfer=new DataTransfer();
    transfer.items.add(new File([Uint8Array.from(atob(data),c=>c.charCodeAt(0))],'拖放.png',{type:'image/png'}));
    document.querySelector('.library-heading').dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:transfer}));
  },image64);
  await editor.waitFor();
  await page.locator('#annotate-cancel').click();
  await editor.waitFor({state:'hidden'});
  await page.waitForTimeout(100);
  await page.locator('.library-heading').hover();
  await page.evaluate(image=>window.whitebgDesktopDrop({images:[image]}),source);
  await editor.waitFor();
  await page.locator('#annotate-new-material').click();
  await page.locator('#annotate-material-name').fill('测试木皮');
  await page.locator('#annotate-material-form button').click();
  await page.waitForFunction(()=>document.querySelector('#annotate-material').value==='测试木皮');
  await page.locator('#annotate-cancel').click();
  await page.locator('[data-keywords="测试木皮"]').click();
  await page.locator('#ref-keywords').fill('测试木皮，胡桃木皮');
  await dialog.getByRole('button',{name:'保存关键词'}).click();
  await dialog.waitFor({state:'hidden'});
  assert.deepEqual((await request('/api/refs')).materials['测试木皮'].keywords,['测试木皮','胡桃木皮']);

  await page.goto(url);
  await page.locator('#storage-cleanup').waitFor();
  await task.waitFor();
  const storage=await request('/api/storage');
  assert.equal(storage.cleanable_tasks,1);
  assert.ok(storage.cleanable_bytes>=28*1024*1024);
  await screenshot('03-home-storage.png');
  await page.locator('#storage-cleanup').click();
  await dialog.getByText('1 个任务').waitFor();
  await screenshot('04-cleanup-confirm.png');
  await page.locator('#storage-cancel').click();
  assert.equal((await request('/api/storage')).cleanable_tasks,1);
  await page.locator('#storage-cleanup').click();
  await page.locator('#storage-confirm').click();
  await task.locator('.pill.cleaned').getByText('已清理',{exact:true}).waitFor();
  await page.locator('#storage-cleanup').waitFor({state:'hidden'});
  assert.equal((await request('/api/storage')).cleanable_tasks,0);
  assert.ok(fs.existsSync(fixture.sku));
  await task.click();
  await page.locator('.item-thumb').getByText('已清理').first().waitFor();
  await page.locator('#calibration-history').click();
  await dialog.getByText('校准记录已清理').waitFor();
  assert.deepEqual(errors,[]);
  console.log('上传、两框保存与刷新、裁图定位、改材质、三框截图、缩放平移、移动和缩放框、删框删图、两种拖放、关键词和清理流程全部通过。');
}

async function run(){
  const connection=net.connect({host:'127.0.0.1',port:8796});
  const occupied=await new Promise(resolve=>{
    connection.once('error',()=>resolve(false));
    connection.once('connect',()=>{connection.destroy();resolve(true);});
  });
  assert.ok(!occupied,'隔离测试端口 8796 已占用。');
  fs.mkdirSync(tmp,{recursive:true});
  fs.mkdirSync(shots,{recursive:true});
  const root=fs.mkdtempSync(path.join(tmp,'wb12-flow-'));
  fs.cpSync(path.join(synced,'web'),path.join(root,'web'),{recursive:true});
  const log=fs.openSync(path.join(root,'service.log'),'w');
  const service=spawn('python.exe',['-B','-u','-c',python,root],{cwd:synced,stdio:['pipe',log,log],windowsHide:true,
    env:{...process.env,PYTHONUTF8:'1',PYTHONDONTWRITEBYTECODE:'1'}});
  fs.closeSync(log);
  const stopped=once(service,'close');
  service.stdin.on('error',()=>{});
  let browser,passed=false;
  try{
    await waitFor(()=>request('/api/health').then(health=>path.resolve(health.root)===root,()=>false),'隔离服务');
    browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
      headless:true,args:['--enable-unsafe-swiftshader','--disable-background-networking']});
    await browserFlow(await browser.newPage({viewport:{width:1600,height:1000},deviceScaleFactor:1}),root);
    passed=true;
  }finally{
    if(browser)await browser.close();
    service.stdin.end('停止\n');
    await Promise.race([stopped,sleep(3000)]);
    if(service.exitCode===null)service.kill();
    await stopped;
    if(passed){
      assert.equal(path.dirname(fs.realpathSync(root)),fs.realpathSync(tmp));
      fs.rmSync(root,{recursive:true});
    }else console.error('隔离现场已保留：'+root);
  }
}
run().catch(error=>{console.error(error);process.exitCode=1;});
