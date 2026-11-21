// 使用本机已有 Edge 与 playwright-core，不安装依赖。
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_CORE||'playwright-core');
const args=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const report={steps:[],console_errors:[],page_errors:[],request_failures:[],uploads:[]};
async function run(){
  const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true,
    args:['--enable-unsafe-swiftshader']});
  const page=await browser.newPage({viewport:{width:1600,height:1000}});
  const shots=process.env.WHITEBG_LIGHTING_SHOTS;
  async function screenshot(name){
    if(!shots)return;
    fs.mkdirSync(shots,{recursive:true});
    await page.screenshot({path:path.join(shots,name)});
  }
  page.on('console',m=>{if(m.type()==='error')report.console_errors.push(m.text());});
  page.on('pageerror',e=>report.page_errors.push(e.message));
  page.on('requestfailed',r=>report.request_failures.push({url:r.url(),error:r.failure()?.errorText}));
  page.on('response',r=>{if(r.url().includes('/lighting-images'))report.uploads.push({url:r.url(),status:r.status()});});
  try{
    await page.goto(args.url);await page.waitForFunction(()=>!!window.__whitebg);
    await page.fill('#product-path',args.product_dir);await page.click('button[type=submit]');await page.locator('#scan-results').waitFor({state:'visible'});
    await page.locator('input[name=model]').evaluateAll((inputs,fbx)=>{for(const i of inputs)i.checked=i.value===fbx;},args.fbx);
    await page.click('#create-task');await page.waitForFunction(()=>window.__whitebg.task&&window.__whitebg.model);
    report.task_id=await page.evaluate(()=>window.__whitebg.task.id);
    await page.waitForFunction(()=>document.querySelector('#loading-model').hidden);
    await page.click('#tab-lighting');
    const direction=page.locator('#lighting-enabled'),controls=page.locator('#lighting-controls');
    await controls.waitFor({state:'hidden'});
    assert.equal(await direction.isChecked(),false);
    assert.deepEqual(await page.locator('#lighting-panel > label').allTextContents(),['这一张单独打光','光方向']);
    await page.locator('#lighting-panel').scrollIntoViewIfNeeded();
    const originalPreview=await page.locator('#canvas').screenshot();
    await screenshot('01-light-off.png');
    await direction.check();
    await controls.waitFor({state:'visible'});
    const defaultLight={enabled:true,azimuth_deg:-45,elevation_deg:35,intensity:1,contrast:.5};
    assert.deepEqual(await page.evaluate(()=>window.__whitebg.task.lighting),defaultLight);
    await page.evaluate(()=>window.__whitebg.flush());
    assert.ok(!(await page.locator('#canvas').screenshot()).equals(originalPreview));
    await screenshot('02-light-on.png');
    report.steps.push('首次开启光方向后保存左前 45°、高 35°、亮度 1.0、对比 0.5。');
    await page.check('#lighting-clay');await direction.uncheck();
    await controls.waitFor({state:'hidden'});
    // 等待恢复模板光的绘制帧。比较原始字节，避免失败时展开整张 PNG。
    let restoredPreview;
    for(let attempt=0;attempt<8;attempt++){
      restoredPreview=await page.locator('#canvas').screenshot();
      if(restoredPreview.equals(originalPreview))break;
    }
    if(!restoredPreview.equals(originalPreview)){
      fs.writeFileSync(path.join(args.run_dir,'template-before.png'),originalPreview);
      fs.writeFileSync(path.join(args.run_dir,'template-after.png'),restoredPreview);
    }
    assert.ok(restoredPreview.equals(originalPreview),
      `切回不打光后的画布与原预览不同（${restoredPreview.length} / ${originalPreview.length} 字节）。`);
    report.steps.push('切回不打光后，画布像素与原预览完全一致。');
    await direction.check();
    await controls.waitFor({state:'visible'});
    // 真实鼠标拖动到圆盘左下方，对应 PF1 左前光。
    const disk=await page.locator('#lighting-disk').boundingBox();
    await page.mouse.move(disk.x+disk.width*.5,disk.y+disk.height*.9);await page.mouse.down();
    await page.mouse.move(disk.x+disk.width*.2,disk.y+disk.height*.8);await page.mouse.up();
    assert.equal(await page.evaluate(()=>window.__whitebg.task.lighting.azimuth_deg),-45);
    // 单张设置整套覆盖，关闭后重新继承统一光。
    await direction.uncheck();
    await page.check('#lighting-single');await direction.check();
    await page.click('[data-light-preset="1"]');await page.evaluate(()=>window.__whitebg.flush());
    assert.equal(await page.evaluate(()=>window.__whitebg.task.items[0].lighting.azimuth_deg),45);
    await direction.uncheck();
    assert.equal(await page.evaluate(()=>window.__whitebg.task.items[0].lighting.enabled),false);
    await direction.check();
    assert.equal(await page.evaluate(()=>window.__whitebg.task.items[0].lighting.azimuth_deg),45);
    assert.equal(await page.evaluate(()=>window.__whitebg.task.lighting.azimuth_deg),-45);
    assert.equal(await page.evaluate(()=>window.__whitebg.task.lighting.enabled),false);
    await page.uncheck('#lighting-single');await page.evaluate(()=>window.__whitebg.flush());
    assert.equal(await page.evaluate(()=>window.__whitebg.task.items[0].lighting),null);
    await controls.waitFor({state:'hidden'});
    await direction.check();
    report.steps.push('单张独立设置与统一设置互不覆盖，关闭独立设置后恢复统一光。');
    await page.click('#smallshot');
    await page.waitForFunction(()=>window.__whitebg.task.items.every(i=>!i.enabled||i.smallshot.state==='done'));
    assert.equal(report.uploads.filter(r=>r.status===201).length,await page.evaluate(()=>window.__whitebg.task.items.filter(i=>i.enabled).length));
    await page.click('#calibration-history');await page.click('#start-calibration');
    await page.waitForFunction(()=>window.__whitebg.task.ai_calibration.state==='waiting_user',null,{timeout:60000});
    assert.ok(await page.evaluate(()=>window.__whitebg.task.items.every(i=>!i.enabled||i.smallshot.state==='done')));
    report.steps.push('假执行器与 fake_codex.py 完成小样和 AI 校准。');
    // 选用最后一轮，然后提交正式渲染。
    await page.click('#calibration-history');await page.locator('[data-accept-round]').last().click();await page.click('#dialog-close');
    await page.click('#final');await page.waitForFunction(()=>window.__whitebg.task.items.every(i=>!i.enabled||i.final.state==='done'),null,{timeout:30000});
    await page.click('#delivery');await page.locator('#confirm-delivery').waitFor();
    assert.match(await page.locator('.delivery-table').innerText(),/光向参考\/1.png/);
    await page.click('#confirm-delivery');await page.getByText('入库完成',{exact:true}).waitFor();await page.click('#dialog-close');
    report.steps.push('正式渲染任务完成并经网页确认入库。');
    await page.locator('#lighting-panel').scrollIntoViewIfNeeded();
    await page.evaluate(()=>{const aside=document.querySelector('.adjustments'),panel=document.querySelector('#lighting-panel');aside.scrollTop=panel.offsetTop-aside.offsetTop-12;});
    await page.locator('#toast').waitFor({state:'hidden'});
    const screen=path.join(args.root,'tests/screens/06-lighting.png');await page.screenshot({path:screen});report.screenshot=screen;
    assert.deepEqual(report.console_errors,[]);assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.request_failures,[]);
    report.passed=true;
  }finally{
    await browser.close();const file=path.join(args.run_dir,'browser-report.json');fs.writeFileSync(file,JSON.stringify(report,null,2));
  }
  console.log(JSON.stringify(report,null,2));
}
run().catch(e=>{console.error(e);process.exitCode=1;});
