// 新版浅色界面截图：输出到 tests\tmp\e2e\ui-shots
const fs=require('node:fs'),path=require('node:path');
const {outDir,url,prepare,withService,withPage,request,until,modelReady,scan,done,readyImages}=require('./harness.cjs');
const directory=path.join(outDir,'ui-shots');
fs.mkdirSync(directory,{recursive:true});
const report={screens:[],console:[],pageErrors:[],httpErrors:[],external:[]};
const only=process.argv.slice(2);
async function take(page,name){
  await page.mouse.move(1,1);
  await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
  await page.waitForTimeout(450);
  await page.screenshot({path:path.join(directory,name)});
  report.screens.push(name);console.log(name);
}
async function newTask(page,demo){
  await scan(page,demo);
  await page.locator('#create-task').click();
  await page.waitForFunction(()=>window.__whitebg?.task);
  return page.evaluate(()=>window.__whitebg.task.id);
}
async function run(){
  await withService(async service=>{
    await service.toggleRunner(true);
    await withPage(async(page,cdp,context)=>{
      page.on('pageerror',e=>report.pageErrors.push(e.message));
      page.on('console',m=>{if(m.type()==='error')report.console.push(m.text());});
      page.on('response',r=>{if(r.status()>=400)report.httpErrors.push({status:r.status(),url:r.url()});});
      page.on('request',r=>{if(!r.url().startsWith(url)&&!/^(blob|data):/.test(r.url()))report.external.push(r.url());});
      // 先建两个旁支任务，让首页有多张不同色调的卡片
      // 首页只列当前产品的任务，所以旁支任务也建在同一个产品上
      const demo=prepare('main');
      for(let k=0;k<2;k++){
        const id=await newTask(page,demo);
        await until(async()=>(await request('api/tasks/'+id)).jobs.some(j=>j.type==='export_preview'),'预览作业');
        service.advance();await modelReady(page);
      }
      await scan(page,demo);
      await page.waitForFunction(()=>document.querySelector('#start-page .runner-status')?.dataset.state==='online');
      await take(page,'02-scan.png');
      await page.locator('#create-task').click();
      await page.waitForFunction(()=>window.__whitebg?.task);
      const taskId=await page.evaluate(()=>window.__whitebg.task.id);
      await until(async()=>(await request('api/tasks/'+taskId)).jobs.some(j=>j.type==='export_preview'),'预览作业');
      service.advance();await modelReady(page);
      await page.locator('#smallshot').click();
      await page.waitForFunction(()=>window.__whitebg.task.items.some(i=>i.smallshot.state==='queued'));
      service.advance(true);
      await page.waitForFunction(()=>/2\/\d+ · 约/.test(document.getElementById('smallshot').textContent));
      await readyImages(page,'#items img');
      await take(page,'04-task-running.png');
      await done(page,'smallshot');await readyImages(page,'#items img');
      await page.locator('#toast').waitFor({state:'hidden'});
      await take(page,'03-task.png');
      await page.locator('#tab-lighting').click();
      await page.locator('#lighting-enabled').check();
      await page.locator('[data-light-preset="0"]').click();
      await page.waitForFunction(()=>document.getElementById('save-state').dataset.state==='saved');
      await take(page,'05-task-lighting.png');
      await page.locator('#lighting-enabled').uncheck();
      await page.locator('#tab-references').click();
      await readyImages(page,'#reference-list img');
      await take(page,'06-task-refs.png');
      await page.locator('#tab-pose').click();
      if(!only.includes('--no-ai')){
        await page.locator('#calibration-history').click();
        await page.locator('#calibration-note').fill('木纹稍暖');
        await page.locator('#start-calibration').click();
        await page.locator('#dialog').waitFor({state:'hidden'});
        await service.toggleRunner(false);await service.toggleRunner(true,true);
        await page.waitForFunction(()=>window.__whitebg.task.ai_calibration.state==='waiting_user',null,{timeout:60000});
        await page.locator('#calibration-history').click();
        await page.locator('.calibration-round').first().waitFor();
        await readyImages(page,'.round-images img');
        await page.locator('#toast').waitFor({state:'hidden'});
        await take(page,'07-calibration.png');
        await page.locator('#dialog-close').click();
      }
      await page.locator('#final').click();await done(page,'final');
      await page.locator('#toast').waitFor({state:'hidden'});
      await page.locator('#delivery').click();await page.locator('#confirm-delivery').waitFor();
      await take(page,'08-delivery.png');
      await page.locator('#dialog-close').click();
      await page.setViewportSize({width:1280,height:800});
      await take(page,'12-task-1280.png');
      await page.setViewportSize({width:1600,height:1000});
      await page.goto(url);
      await page.locator('#task-list .task-card').nth(2).waitFor();
      await readyImages(page,'#task-list img');
      await take(page,'01-home.png');
      await page.evaluate(()=>document.querySelector('.pages').scrollTo(0,99999));
      await take(page,'01c-home-tasks.png');
      await page.evaluate(()=>document.querySelector('.pages').scrollTo(0,0));
      await page.locator('.rail-item[data-nav=refs]').hover();
      await page.waitForTimeout(300);
      await page.screenshot({path:path.join(directory,'01b-rail-hover.png'),clip:{x:0,y:0,width:420,height:360}});
      await page.locator('.rail-item[data-nav=refs]').click();
      await page.locator('.ref-card img').first().waitFor();
      await page.waitForFunction(()=>[...document.querySelectorAll('.ref-card img')].filter(i=>i.getBoundingClientRect().top<innerHeight).every(i=>i.complete&&i.naturalWidth>0));
      await take(page,'09-refs.png');
      await page.locator('.rail-item[data-nav=settings]').click();
      await page.locator('#settings-page').waitFor();
      await take(page,'10-settings.png');
      await page.locator('#ai-settings-entry').click();
      await page.locator('#ai-settings-dialog').waitFor();
      await page.waitForFunction(()=>!document.querySelector('input[name=ai-engine][value=openai_api]').disabled);
      await page.locator('input[name=ai-engine][value=openai_api]').check({force:true});
      await take(page,'11-ai-settings.png');
    });
  });
}
run().catch(e=>{report.failure=e.stack;console.error(e);process.exitCode=1;}).finally(()=>{
  fs.mkdirSync(outDir,{recursive:true});
  fs.writeFileSync(path.join(outDir,'ui-shots.json'),JSON.stringify(report,null,2));
});
