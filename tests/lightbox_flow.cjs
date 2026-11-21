const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_CORE||'playwright-core');
const args=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const report={入口:[],控制台错误:[],页面错误:[],交互:[]};
const preserve=f=>{if(fs.existsSync(f))fs.copyFileSync(f,f+'.'+Date.now()+'.bak');};
async function run(){
 const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
 const page=await browser.newPage({viewport:{width:1600,height:1100}});
 page.on('console',m=>{if(m.type()==='error')report.控制台错误.push(m.text());});page.on('pageerror',e=>report.页面错误.push(e.message));
 const box=page.locator('dialog.lightbox');
 const loaded=()=>page.waitForFunction(()=>{const b=document.querySelector('dialog.lightbox');return b?.open&&b.querySelector('img')?.naturalWidth>0&&!b.querySelector('img').hidden;});
 async function close(key=true){if(key)await page.keyboard.press('Escape');else await box.locator('[data-close]').click();await page.waitForFunction(()=>!document.querySelector('dialog.lightbox').open&&document.body.style.overflow==='');}
 async function check(selector,name){await page.locator(selector).first().click();await loaded();report.入口.push(name+'：打开成功');}
 try{
  await page.goto(args.url);await page.waitForFunction(()=>!!window.__whitebg);
  await page.fill('#product-path',args.product_dir);await page.click('button[type=submit]');await page.locator('#scan-results').waitFor();
  await page.locator('input[name=model]').evaluateAll((inputs,fbx)=>{for(const i of inputs)i.checked=i.value===fbx;},args.fbx);
  await page.click('#create-task');await page.waitForFunction(()=>window.__whitebg.task&&window.__whitebg.model);
  report.task_id=await page.evaluate(()=>window.__whitebg.task.id);
  // 新任务只起草主图：用「加一张」菜单加正面、侧面、俯视，凑回 4 张（后面要在灯箱里翻页）
  for(const [index,name] of ['正面','侧面','俯视'].entries()){
    await page.click('#add-item');await page.locator('#view-picker').waitFor({state:'visible'});
    await page.fill('#view-search',name);await page.keyboard.press('Enter');
    await page.waitForFunction(n=>window.__whitebg.task.items.length===n,index+2);
  }
  await page.evaluate(()=>window.__whitebg.flush());
  // 新加的那张会被选中；回到第一张，后面要验证点第二行缩略图不改变选中
  await page.locator('#items .item-row').nth(0).locator('.item-info').click();
  await page.waitForFunction(()=>document.querySelector('#items .item-row.selected')?.dataset.index==='0');
  await page.click('#smallshot');
  await page.waitForFunction(()=>window.__whitebg.task.items.every(i=>!i.enabled||i.smallshot.state==='done'));
  await page.click('#calibration-history');await page.click('#start-calibration');
  await page.waitForFunction(()=>window.__whitebg.task.ai_calibration.state==='waiting_user',null,{timeout:60000});
  // 点第二行缩略图不会改变第一行的选中状态。
  await page.locator('#items .item-row').nth(1).locator('img').click();await loaded();
  assert.equal(await page.locator('#items .item-row.selected').getAttribute('data-index'),'0');
  report.入口.push('任务清单缩略图：打开成功，未切换条目');
  const firstCaption=await box.locator('.lightbox-caption').innerText();
  await page.keyboard.press('ArrowRight');await loaded();assert.notEqual(await box.locator('.lightbox-caption').innerText(),firstCaption);
  await box.locator('.lightbox-prev').click();await loaded();assert.equal(await box.locator('.lightbox-caption').innerText(),firstCaption);
  await box.locator('[data-original]').click();assert.equal(await box.locator('output').innerText(),'100%');
  const stage=await box.locator('.lightbox-stage').boundingBox();
  await page.mouse.move(stage.x+stage.width/2,stage.y+stage.height/2);await page.mouse.wheel(0,-Math.log(2)/.002);
  await page.waitForFunction(()=>document.querySelector('dialog.lightbox output').textContent==='200%');
  const screen=path.join(args.root,'tests/screens/07-lightbox.png');await page.screenshot({path:screen});report.截图=screen;
  const before=await box.locator('img').evaluate(i=>i.style.transform);
  await page.mouse.down();await page.mouse.move(stage.x+stage.width/2+70,stage.y+stage.height/2+40);await page.mouse.up();
  assert.notEqual(await box.locator('img').evaluate(i=>i.style.transform),before);
  await box.locator('img').dblclick();assert.equal(await box.locator('output').innerText(),'100%');
  await close();assert.equal(await page.evaluate(()=>document.activeElement?.dataset.previewGroup),'task-items');assert.equal(await page.evaluate(()=>document.body.style.overflow),'');
  report.交互.push('左右键与按钮翻页、滚轮缩放到 200%、左键拖动、双击、Esc、恢复焦点与滚动通过');
  await page.locator('#items .item-row').nth(1).locator('.item-info').click();assert.equal(await page.locator('#items .item-row.selected').getAttribute('data-index'),'1');
  await page.click('#tab-references');await page.locator('#reference-list [data-ref]').nth(1).click();assert.equal(await page.locator('#reference-list [data-ref].selected').getAttribute('data-ref'),'1');
  await page.locator('.reference-choice').first().hover();await check('.reference-zoom','右栏参考图放大按钮');
  assert.equal(await page.locator('#reference-list [data-ref].selected').getAttribute('data-ref'),'1');await close(false);
  await page.click('#overview');await check('.overview-card[data-preview]','总览卡片');await close();assert.equal(await page.locator('#dialog').evaluate(d=>d.open),true);await page.click('#dialog-close');
  await page.click('#calibration-history');await check('.round-images img','AI 校准');
  assert.match(await box.locator('.lightbox-caption').innerText(),/第 \d+ 轮 · \d+ ·/);
  const initial=await box.locator('.lightbox-caption').innerText();
  for(let i=0;i<8;i++){await page.keyboard.press('ArrowRight');await loaded();}
  assert.notEqual(await box.locator('.lightbox-caption').innerText(),initial);await close();await page.click('#dialog-close');
  await page.click('#go-home');await check('#start-refs img','首页参考图');await close();
  await page.click('a[href="#refs"]');await page.locator('#refs-page').waitFor();await check('.ref-group img','参考库图片');await close();
  const firstGroup=page.locator('.ref-group').first();
  const statuses=['approved','rejected'];
  if(!await firstGroup.locator('[data-ref-status="approved"]:enabled').count())statuses.reverse();
  for(const status of statuses){
    const button=firstGroup.locator(`[data-ref-status="${status}"]:enabled`).first();
    const file=await button.getAttribute('data-file');
    await button.click();
    await firstGroup.locator(`[data-ref-status="${status}"][data-file=${JSON.stringify(file)}]:disabled`).waitFor();
  }
  report.交互.push('清单行切换、参考图选中、参考库认可和不用按钮保持有效');
  // 直接接口单张、加载中和失败状态，避免发送故意失败的 HTTP 请求。
  await page.route('**/api/slow-lightbox',()=>{});
  await page.evaluate(async()=>{const {openLightbox}=await import('/lightbox.js');openLightbox([{src:'/api/slow-lightbox',caption:'单张测试'}],0);});
  assert.equal(await box.locator('.lightbox-next').isVisible(),false);
  assert.equal(await box.locator('.lightbox-status').innerText(),'加载中…');await close();
  await page.evaluate(async()=>{const {openLightbox}=await import('/lightbox.js');openLightbox([{src:'data:image/png;base64,AA==',caption:'失败测试'}],0);});
  await page.waitForFunction(()=>document.querySelector('.lightbox-status').textContent==='图片打不开');
  await page.mouse.click(80,80);await page.waitForFunction(()=>!document.querySelector('dialog.lightbox').open);
  report.交互.push('单张隐藏翻页、加载中、加载失败、遮罩关闭通过');
  assert.deepEqual(report.控制台错误,[]);assert.deepEqual(report.页面错误,[]);report.通过=true;
 }finally{await browser.close();const file=path.join(args.run_dir,'browser-report.json');fs.writeFileSync(file,JSON.stringify(report,null,2));}
 console.log(JSON.stringify(report,null,2));
}
run().catch(e=>{console.error(e);process.exitCode=1;});
