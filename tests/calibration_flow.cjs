// 使用本机已有 Edge 和 playwright-core，不安装依赖。
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_CORE||'playwright-core');
const args=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const report={steps:[],console_errors:[],page_errors:[],request_failures:[],external_requests:[]};

async function run(){
  const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
  const page=await browser.newPage({viewport:{width:1600,height:1100}});
  page.on('console',m=>{if(m.type()==='error')report.console_errors.push(m.text());});
  page.on('pageerror',e=>report.page_errors.push(e.message));
  page.on('requestfailed',r=>report.request_failures.push({url:r.url(),error:r.failure()?.errorText}));
  page.on('request',r=>{if(!r.url().startsWith(args.url)&&!r.url().startsWith('blob:')&&!r.url().startsWith('data:'))report.external_requests.push(r.url());});
  async function snap(name){const file=path.join(args.root,'tests','screens',name);await page.screenshot({path:file});}
  try{
    await page.goto(args.url);await page.waitForFunction(()=>!!window.__whitebg);
    await page.fill('#product-path',args.product_dir);await page.click('button[type=submit]');
    await page.locator('#scan-results').waitFor({state:'visible'});
    await page.locator('input[name=model]').evaluateAll((inputs,fbx)=>{for(const input of inputs)input.checked=input.value===fbx;},args.fbx);
    await page.click('#create-task');await page.waitForFunction(()=>window.__whitebg.task&&window.__whitebg.model);
    report.task_id=await page.evaluate(()=>window.__whitebg.task.id);
    await page.click('#smallshot');
    await page.waitForFunction(()=>window.__whitebg.task.items.every(i=>!i.enabled||i.smallshot.state==='done'));
    assert.deepEqual(await page.evaluate(()=>window.__whitebg.task.ai_calibration.rounds),[]);
    await page.click('#calibration-history');
    await page.fill('#calibration-note','木纹稍暖');
    await page.click('#start-calibration');
    await page.locator('#dialog').waitFor({state:'hidden'});
    assert.equal(await page.locator('.workspace').evaluate(el=>el.inert),false);
    await page.waitForFunction(()=>window.__whitebg.task.ai_calibration.state==='waiting_user',null,{timeout:60000});
    let calibration=await page.evaluate(()=>window.__whitebg.task.ai_calibration);
    assert.deepEqual(calibration.rounds.map(r=>r.verdict),['adjust','adjust','ok']);
    report.steps.push('出小样未启动校准。手动开始后完成三轮，进入待确认。');
    await page.click('#calibration-history');await page.locator('.calibration-round').nth(2).waitFor();
    await page.waitForFunction(()=>[...document.querySelectorAll('.round-images img')].every(i=>i.complete&&i.naturalWidth>0));
    assert.equal(await page.locator('.calibration-round').count(),3);
    assert.equal(await page.locator('.calibration-round').nth(1).locator('img').count(),await page.evaluate(()=>window.__whitebg.task.items.filter(i=>i.enabled).length));
    assert.doesNotMatch(await page.locator('#dialog-content').innerText(),/hue_shift_deg|brightness_mul|reflection_mul/);
    await page.click('[data-accept-round="2"]');
    await page.waitForFunction(()=>window.__whitebg.task.ai_calibration.accepted_round===2);
    assert.match(await page.locator('#accepted-dot').getAttribute('title'),/已采用第 2 轮调整/);
    await page.locator('#dialog-content').evaluate(e=>e.scrollTop=e.scrollHeight);
    await page.locator('#dialog').evaluate(e=>e.scrollTop=e.scrollHeight);
    await snap('04-calibration.png');
    report.steps.push('记录显示每轮全部小样、中文结论与改动和置信度；选用第 2 轮后显示正式渲染提示。');
    await page.click('#undo-calibration');await page.waitForFunction(()=>window.__whitebg.task.ai_calibration.accepted_round===null);
    await page.click('[data-accept-round="2"]');await page.waitForFunction(()=>window.__whitebg.task.ai_calibration.accepted_round===2);
    await page.click('#dialog-close');await page.click('#final');
    await page.waitForFunction(()=>window.__whitebg.task.items.every(i=>!i.enabled||i.final.state==='done'),null,{timeout:30000});
    report.steps.push('撤销并重新采用第 2 轮后提交正式渲染，假执行器完成全部成品。');
    await page.click('#go-home');await page.click('a[href="#refs"]');await page.locator('#refs-page').waitFor({state:'visible'});
    const first=page.locator('.ref-group').first();
    await first.locator('.ref-card').first().waitFor();
    const statuses=['approved','rejected'];
    if(!await first.locator('[data-ref-status="approved"]:enabled').count())statuses.reverse();
    for(const status of statuses){
      const button=first.locator(`[data-ref-status="${status}"]:enabled`).first();
      const file=await button.getAttribute('data-file');
      await button.click();
      await first.locator(`[data-ref-status="${status}"][data-file=${JSON.stringify(file)}]:disabled`).waitFor();
    }
    const buffer=fs.readFileSync(path.join(args.product_dir,'参考图','主图.png'));
    await page.locator('[data-ref-upload]').first().setInputFiles({name:'上传白底.png',mimeType:'image/png',buffer});
    await page.waitForFunction(()=>document.querySelector('.ref-group').textContent.includes('上传白底.png'));
    await page.evaluate(encoded=>{
      const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));
      const transfer=new DataTransfer();transfer.items.add(new File([bytes],'拖入白底.png',{type:'image/png'}));
      document.querySelector('.ref-group').dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer}));
    },buffer.toString('base64'));
    await page.waitForFunction(()=>document.querySelector('.ref-group').textContent.includes('拖入白底.png'));
    await page.locator('[data-ref-path]').first().fill(path.join(args.product_dir,'参考图','主图.png'));
    await page.locator('[data-add-ref-path]').first().click();
    await page.waitForFunction(()=>document.querySelector('.ref-group').textContent.includes('主图.png'));
    await page.waitForFunction(()=>[...document.querySelectorAll('.ref-group:first-child img')].every(i=>i.complete&&i.naturalWidth>0));
    await snap('05-refs.png');
    report.steps.push('参考库认可、不用、内容上传、浏览器拖图和完整路径添加均成功，使用隔离参考库。');
    assert.deepEqual(report.console_errors,[]);assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.request_failures,[]);assert.deepEqual(report.external_requests,[]);
  }finally{await browser.close();}
}
run().catch(error=>{report.failure=String(error.stack||error);process.exitCode=1;}).finally(()=>{
  const file=path.join(args.run_dir,'browser-report.json');fs.writeFileSync(file,JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
});
