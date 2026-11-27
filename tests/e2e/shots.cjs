const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {outDir,url,prepare,withService,withPage,request,until,modelReady,scan,done,readyImages}=require('./harness.cjs');
const directory=path.join(outDir,'shots');
fs.mkdirSync(directory,{recursive:true});
const report={screens:[],console:[],pageErrors:[],httpErrors:[],external:[],checks:[]};
async function take(page,name){
  await page.mouse.move(1,1);
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await page.screenshot({path:path.join(directory,name)});
  report.screens.push(name);
  console.log(name);
}
async function libraryReady(page){
  await page.locator('.ref-card img').first().waitFor();
  await page.waitForFunction(()=>[...document.querySelectorAll('.ref-card img')].filter(image=>{
    const bounds=image.getBoundingClientRect();
    return bounds.top<innerHeight&&bounds.bottom>0;
  }).every(image=>image.complete&&image.naturalWidth>0));
}
async function refsOnly(){
  await withService(async()=>withPage(async page=>{
    await page.goto(url+'#refs');
    await libraryReady(page);
    await take(page,'10-refs.png');
  }));
}
async function run(){
  await withService(async service=>{
    const demo=prepare('shots');
    await service.toggleRunner(true);
    await withPage(async (page,cdp,context)=>{
      page.on('pageerror',e=>report.pageErrors.push(e.message));
      page.on('console',m=>{if(m.type()==='error')report.console.push(m.text());});
      page.on('response',r=>{if(r.status()>=400)report.httpErrors.push({status:r.status(),url:r.url()});});
      page.on('request',r=>{if(!r.url().startsWith(url)&&!r.url().startsWith('blob:')&&!r.url().startsWith('data:'))report.external.push(r.url());});
      await scan(page,demo);
      await page.waitForFunction(()=>document.querySelector('#start-page .runner-status')?.dataset.state==='online');
      await take(page,'01-start.png');

      await service.toggleRunner(false);
      let maxRunning=true,starting=0;
      const offlineRoute=route=>route.fulfill({json:{online:false,max_running:maxRunning,can_start:!maxRunning}});
      await page.route('**/api/runner',offlineRoute);
      await page.route('**/api/runner/start',route=>{starting++;return route.fulfill({json:{started:true}});});
      await page.waitForFunction(()=>document.querySelector('#start-page .runner-status').dataset.state==='offline');
      await page.locator('#start-page .runner-chip').click();
      await context.grantPermissions(['clipboard-read','clipboard-write']);
      await page.locator('#start-page .runner-copy').click();
      await page.getByText('已复制脚本路径',{exact:true}).waitFor();
      assert.match(await page.evaluate(()=>navigator.clipboard.readText()),/\\max\\启动白底执行器\.ms$/);
      await page.locator('#toast').waitFor({state:'hidden'});
      await take(page,'02-start-offline.png');
      maxRunning=false;
      await page.locator('#start-page .runner-start').click();
      await page.locator('#start-page .runner-panel').getByText('正在启动 Max…').waitFor();
      assert.equal(starting,1);
      await page.unroute('**/api/runner',offlineRoute);
      await service.toggleRunner(true);
      await page.waitForFunction(()=>document.querySelector('#start-page .runner-status').dataset.state==='online');
      report.checks.push('执行器离线面板可复制脚本路径，启动请求进入等待状态，收到在线心跳后恢复。启动请求由测试替身响应。');

      await page.locator('#create-task').click();
      await page.waitForFunction(()=>window.__whitebg?.task);
      const taskId=await page.evaluate(()=>window.__whitebg.task.id);
      await until(async()=>{
        const {jobs}=await request('api/tasks/'+taskId);
        return jobs.some(job=>job.type==='export_preview');
      },'预览作业提交');
      service.advance();
      await modelReady(page);
      await page.locator('#smallshot').click();
      await page.waitForFunction(()=>window.__whitebg.task.items.some(i=>i.smallshot.state==='queued'));
      service.advance(true);
      await page.waitForFunction(()=>/2\/\d+ · 约/.test(document.getElementById('smallshot').textContent));
      await readyImages(page,'#items img');
      await take(page,'06-task-running.png');
      await done(page,'smallshot');
      await readyImages(page,'#items img');
      await page.locator('#toast').waitFor({state:'hidden'});
      await take(page,'03-task.png');

      await page.locator('#tab-lighting').click();
      await page.locator('[data-light-preset="0"]').click();
      await page.waitForFunction(()=>document.querySelector('[data-light-preset="0"]').classList.contains('active')
        &&document.getElementById('save-state').dataset.state==='saved');
      await take(page,'04-task-lighting.png');
      await page.locator('#tab-references').click();
      await page.locator('#overlay-on').check();
      await readyImages(page,'#overlay,#reference-list img');
      await take(page,'05-task-refs.png');
      await page.reload();
      await modelReady(page);
      assert.equal(await page.locator('#tab-references').getAttribute('aria-selected'),'true');
      assert.equal(await page.locator('#overlay-on').isChecked(),true);
      await page.locator('#overlay-on').uncheck();
      await page.locator('#tab-pose').click();
      report.checks.push('按任务恢复参考图页签和叠加开关。');

      await page.locator('#calibration-history').click();
      await page.locator('#calibration-note').fill('木纹稍暖');
      await page.locator('#start-calibration').click();
      await page.locator('#dialog').waitFor({state:'hidden'});
      await page.waitForFunction(()=>document.querySelector('#calibration-history').classList.contains('is-busy'));
      const camera=await page.locator('#canvas').boundingBox();
      const before=await page.evaluate(()=>window.__whitebg.task.items[0].camera.azimuth_deg);
      await page.mouse.move(camera.x+camera.width/2,camera.y+camera.height/2);
      await page.mouse.down();await page.mouse.move(camera.x+camera.width/2+10,camera.y+camera.height/2);await page.mouse.up();
      await page.evaluate(()=>window.__whitebg.flush());
      assert.notEqual(await page.evaluate(()=>window.__whitebg.task.items[0].camera.azimuth_deg),before);
      report.checks.push('校准中拖动画布并保存成功。');
      await service.toggleRunner(false);
      await service.toggleRunner(true,true);
      await page.waitForFunction(()=>window.__whitebg.task.ai_calibration.state==='waiting_user',null,{timeout:60000});
      await page.locator('#calibration-history').click();
      await page.locator('.calibration-round').first().waitFor();
      await readyImages(page,'.round-images img');
      await page.locator('#toast').waitFor({state:'hidden'});
      await take(page,'07-calibration.png');
      await page.locator('#dialog-close').click();

      await page.locator('#final').click();await done(page,'final');
      await page.locator('#toast').waitFor({state:'hidden'});
      await page.locator('#delivery').click();
      await page.locator('#confirm-delivery').waitFor();
      await take(page,'08-delivery.png');
      await page.locator('#dialog-close').click();
      await page.setViewportSize({width:1280,height:800});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),1280);
      await take(page,'09-task-1280.png');
      await page.setViewportSize({width:1600,height:1000});
      await page.locator('#go-home').click();
      await page.locator('.refs-entry').click();
      await libraryReady(page);
      await take(page,'10-refs.png');
      for(const key of ['console','pageErrors','httpErrors','external'])assert.deepEqual(report[key],[]);
    });
  });
}
const onlyLibrary=process.argv.includes('--refs');
(onlyLibrary?refsOnly():run()).catch(error=>{report.failure=error.stack;console.error(error);process.exitCode=1;}).finally(()=>{
  fs.mkdirSync(outDir,{recursive:true});
  fs.writeFileSync(path.join(outDir,onlyLibrary?'refs-shot.json':'shots.json'),JSON.stringify(report,null,2));
});
