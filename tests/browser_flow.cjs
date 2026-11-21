// 用本机已有 playwright-core 与 Edge 打开实际网页；不安装依赖。
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_CORE||'playwright-core');
const args=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const {root,product_dir,url,run_dir,panel_screenshot}=args;
const screensDir=args.screens_dir||path.join(root,'tests/screens');
const report={steps:[],console_errors:[],page_errors:[],request_failures:[],external_requests:[]};

function json(file,value){fs.writeFileSync(file,JSON.stringify(value,null,2));}
async function run(){
  report.product_dir=product_dir;
  const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
  const page=await browser.newPage({viewport:{width:1600,height:1100},deviceScaleFactor:1});
  page.on('console',m=>{if(m.type()==='error')report.console_errors.push(m.text());});
  page.on('pageerror',e=>report.page_errors.push(e.message));
  page.on('requestfailed',r=>report.request_failures.push({url:r.url(),error:r.failure()?.errorText}));
  page.on('request',r=>{if(!r.url().startsWith(url)&&!r.url().startsWith('blob:')&&!r.url().startsWith('data:'))report.external_requests.push(r.url());});
  try{
    await page.goto(url);
    await page.fill('#product-path',product_dir);await page.click('button[type=submit]');await page.locator('#scan-results').waitFor({state:'visible'});
    report.steps.push('首页扫描到测试 Max 和参考图。');
    await page.click('#create-task');await page.waitForFunction(()=>window.__whitebg?.task);
    report.task_id=await page.evaluate(()=>window.__whitebg.task.id);
    await page.waitForFunction(()=>window.__whitebg?.model,null,{timeout:30000});
    report.export_model=await page.evaluate(()=>window.__whitebg.model);report.steps.push('export_preview 作业完成，网页读到预览模型。');
    await page.waitForFunction(()=>window.__whitebg?.task?.preview_source==='export'&&
      window.__whitebg.task.model_max.toLowerCase().endsWith('.max')&&document.querySelector('#rotate-model').hidden);
    await page.click('[data-orientation="landscape"]');await page.evaluate(()=>window.__whitebg.flush());
    report.landscape_frame=await page.evaluate(()=>{const r=document.querySelector('#canvas').getBoundingClientRect();return {width:r.width,height:r.height};});
    if(Math.abs(report.landscape_frame.width/report.landscape_frame.height-4/3)>1e-8)throw new Error('横版画布比例不准确。');
    await page.click('[data-orientation="portrait"]');await page.evaluate(()=>window.__whitebg.flush());
    await page.click('#tab-references');await page.click('#overlay-on');await page.fill('#opacity','25');await page.dispatchEvent('#opacity','input');
    await page.waitForFunction(()=>{const i=document.querySelector('#overlay');return !i.hidden&&i.complete&&i.naturalWidth>0;});
    // 验证画布尺寸严格服从条目画幅。
    report.frame=await page.evaluate(()=>{const r=document.querySelector('#canvas').getBoundingClientRect();return {width:r.width,height:r.height,orientation:window.__whitebg.task.items[0].orientation};});
    if(Math.abs(report.frame.width/report.frame.height-3/4)>1e-8)throw new Error('竖版画布比例不准确。');
    await page.click('#tab-pose');
    const correction=page.locator('#vertical-correction');
    const camera=()=>page.evaluate(()=>window.__whitebg.task.items[0].camera);
    const beforeCorrection=await camera();
    if(await correction.isChecked())throw new Error('镜头校正应默认关闭。');
    await correction.check();await page.evaluate(()=>window.__whitebg.flush());
    const afterCorrection=await camera();
    if(!afterCorrection.vertical_correction||afterCorrection.distance_mm===beforeCorrection.distance_mm||
      ['azimuth_deg','elevation_deg','fov_y_deg'].some(key=>Math.abs(beforeCorrection[key]-afterCorrection[key])>1e-9))
      throw new Error('打开镜头校正应重新取景并保持转动角、俯视角和透视。');
    const savedCorrection=await (await page.request.get(url+'api/tasks/'+report.task_id)).json();
    if(!savedCorrection.task.items[0].camera.vertical_correction)throw new Error('镜头校正未保存。');
    // 新任务只起草主图：用「加一张」菜单搜索「正面」并回车添加第 2 张
    if(await page.locator('.item-row').count()!==1)throw new Error('新任务应只有主图一张。');
    await page.click('#add-item');await page.locator('#view-picker').waitFor({state:'visible'});
    await page.fill('#view-search','正面');await page.keyboard.press('Enter');
    await page.waitForFunction(()=>window.__whitebg.task.items.length===2&&window.__whitebg.task.items[1].label==='正面'&&document.querySelector('#view-picker').hidden);
    await page.evaluate(()=>window.__whitebg.flush());
    await page.locator('.item-row').nth(1).click();
    await page.waitForFunction(()=>document.querySelector('.item-row.selected')?.dataset.index==='1'&&
      !document.querySelector('#vertical-correction').checked);
    await page.locator('.item-row').nth(0).click();
    await page.waitForFunction(()=>document.querySelector('.item-row.selected')?.dataset.index==='0'&&
      document.querySelector('#vertical-correction').checked);
    const elevation=page.locator('#control-elevation');
    await elevation.fill('46');await elevation.dispatchEvent('input');
    await page.locator('#vertical-correction-note').waitFor({state:'visible'});
    if(!await correction.isDisabled())throw new Error('俯视超过 45° 应禁用镜头校正。');
    await elevation.fill('45');await elevation.dispatchEvent('input');
    await page.locator('#vertical-correction-note').waitFor({state:'hidden'});
    if(await correction.isDisabled())throw new Error('俯视 45° 应允许镜头校正。');
    await page.click('[data-preset="7"]');
    if(!await correction.isChecked())throw new Error('自动取景应保留镜头校正。');
    const beforeOff=await camera();
    await correction.uncheck();
    const correctionOff=await camera();
    if(correctionOff.vertical_correction||correctionOff.distance_mm===beforeOff.distance_mm||
      Math.abs(correctionOff.elevation_deg-45)>1e-9)throw new Error('关闭镜头校正应重新取景并保持俯视角。');
    await correction.check();
    if(panel_screenshot){
      await page.setViewportSize({width:1280,height:800});
      await correction.scrollIntoViewIfNeeded();
      report.correction_layout=await page.evaluate(()=>{
        const panel=document.querySelector('.adjustments'),row=document.querySelector('.correction-switch');
        const bounds=panel.getBoundingClientRect(),rect=row.getBoundingClientRect();
        return {width:innerWidth,height:innerHeight,
          fits:panel.scrollWidth<=panel.clientWidth&&row.scrollWidth<=row.clientWidth&&
            rect.left>=bounds.left&&rect.right<=bounds.right&&rect.top>=bounds.top&&rect.bottom<=Math.min(bounds.bottom,innerHeight)};
      });
      if(!report.correction_layout.fits)throw new Error('1280×800 下镜头校正行溢出。');
      await page.screenshot({path:panel_screenshot});
      await page.setViewportSize({width:1600,height:1100});
    }
    report.steps.push('镜头校正支持逐图保存、45° 限制和自动取景。');
    // 验证调整与开合自动保存，并在切条目后恢复。
    await page.locator('#control-azimuth').fill('42');await page.dispatchEvent('#control-azimuth','input');
    await page.evaluate(()=>window.__whitebg.flush());await page.locator('.item-row').nth(1).click();await page.locator('.item-row').nth(0).click();
    const restored=await page.evaluate(()=>window.__whitebg.task.items[0].camera.azimuth_deg);
    if(Math.abs(restored-42)>.01)throw new Error('切换条目未恢复视角。');
    await page.click('[data-preset="8"]');await page.evaluate(()=>window.__whitebg.flush());
    if(await correction.isChecked())throw new Error('重置应关闭镜头校正。');
    if(await page.locator('#parts-controls input').count()){
      await page.evaluate(()=>{document.getElementById('parts-details').open=true;});
      await page.locator('#parts-controls input').first().fill('60');await page.locator('#parts-controls input').first().dispatchEvent('input');await page.evaluate(()=>window.__whitebg.flush());await page.click('#close-parts');await page.evaluate(()=>window.__whitebg.flush());
      report.steps.push('PZ 开合控件已操作，全部合上已保存。');
    }
    await page.click('#smallshot');
    await page.waitForFunction(()=>window.__whitebg.task.items.every(i=>!i.enabled||i.smallshot.state==='done'),{timeout:30000});
    report.steps.push('出小样全部完成。');
    await page.click('#final');
    await page.waitForFunction(()=>window.__whitebg.task.items.every(i=>!i.enabled||i.final.state==='done'),{timeout:30000});
    report.steps.push('成品及遮罩任务全部完成。');
    await page.click('#overview');await page.locator('.overview-card img').first().waitFor();
    report.overview_count=await page.locator('.overview-card img').count();
    await page.waitForFunction(()=>[...document.querySelectorAll('.overview-card img')].every(i=>i.complete&&i.naturalWidth>0));
    report.steps.push('总览实际显示占位成品。');await page.click('#dialog-close');
    await page.click('#delivery');await page.locator('#confirm-delivery').waitFor();
    report.first_plan=await page.locator('.delivery-table').innerText();
    await page.click('#confirm-delivery');await page.getByText('入库完成',{exact:true}).waitFor();await page.click('#dialog-close');
    await page.click('#delivery');await page.locator('#confirm-delivery').waitFor();
    report.second_plan=await page.locator('.delivery-table').innerText();await page.click('#confirm-delivery');await page.getByText('入库完成',{exact:true}).waitFor();await page.click('#dialog-close');
    const sku=path.join(product_dir,'sku');for(const f of ['1.jpg','1_遮罩_ID.jpg','白底清单.json'])if(!fs.existsSync(path.join(sku,f)))throw new Error('入库缺少文件：'+f);
    report.archive_dirs=fs.readdirSync(path.join(sku,'旧版'));
    if(!report.archive_dirs.length)throw new Error('第二次入库没有归档旧文件。');
    report.steps.push('两次页面确认入库完成，sku 成品、遮罩、清单及旧版均存在。');
    const denied=await fetch(url+'api/file?'+new URLSearchParams({path:'C:\\Windows\\win.ini',task_id:report.task_id}));
    report.forbidden_status=denied.status;if(denied.status!==403)throw new Error('越界文件请求未返回 403。');
    fs.mkdirSync(screensDir,{recursive:true});
    await page.evaluate(()=>document.querySelector('.adjustments').scrollTop=document.querySelector('.adjustments').scrollHeight);
    await page.locator('#toast').waitFor({state:'hidden'});
    await page.waitForTimeout(250);
    const taskScreen=path.join(screensDir,'02-task.png');await page.screenshot({path:taskScreen});
    await page.click('#go-home');await page.locator('#start-page').waitFor({state:'visible'});
    await page.waitForFunction(()=>document.querySelector('#sku-files').textContent.includes('1.jpg'));
    report.steps.push('返回首页后重新扫描，已入库文件正常显示。');
    const startScreen=path.join(screensDir,'01-start.png');await page.screenshot({path:startScreen});
    report.steps.push('实际首页和任务页截图已保存。');
    if(report.console_errors.length||report.page_errors.length||report.request_failures.length||report.external_requests.length)throw new Error('浏览器检查发现错误或外网请求。');
    report.passed=true;
  } finally{
    await browser.close();json(path.join(run_dir,'browser-report.json'),report);
  }
  console.log(JSON.stringify(report,null,2));
}
run().catch(e=>{console.error(e);process.exitCode=1;});
