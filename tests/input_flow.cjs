// 真实浏览器验收上传与路径接入；直接调用桥接接收函数不代表原生拖放验收。
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_CORE||'playwright-core');
const args=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const report={steps:[],page_errors:[],console_errors:[],external_requests:[]};

async function run(){
  const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
  const page=await browser.newPage({viewport:{width:1600,height:1000}});
  page.on('pageerror',error=>report.page_errors.push(error.message));
  page.on('console',message=>{if(message.type()==='error')report.console_errors.push(message.text());});
  page.on('request',request=>{if(!request.url().startsWith(args.url)&&!request.url().startsWith('blob:')&&!request.url().startsWith('data:'))report.external_requests.push(request.url());});
  const image={name:'同名.png',mimeType:'image/png',buffer:fs.readFileSync(args.reference)};
  try{
    await page.goto(args.url);
    await page.waitForFunction(()=>!!window.__whitebg);
    assert.equal(await page.locator('#desktop-buttons').isVisible(),false);
    assert.equal(await page.locator('#browse').isVisible(),true);
    assert.match(await page.locator('#drop-hint').textContent(),/浏览器里不能拖文件夹，请用下面的按钮/);
    await page.fill('#product-path',args.product_dir);
    await page.click('#scan-form button[type=submit]');
    await page.locator('#scan-results').waitFor({state:'visible'});
    assert.match(await page.locator('#sku-hint').textContent(),/已有 1 个文件/);
    report.steps.push('浏览器隐藏原生按钮，保留浏览文件夹；已有 sku 文件夹显示文件数。');

    await page.evaluate(args=>window.whitebgDesktopDrop({product:{product_dir:args.product_dir,selected_model:args.max_file},images:[args.reference],ignored:0,missing:0}),args);
    await page.waitForFunction(max=>document.querySelector('input[name=model]:checked')?.value===max,args.max_file);
    await page.waitForFunction(()=>document.getElementById('pending-refs').textContent.includes('1 张参考图'));
    await page.setInputFiles('#start-reference-input',image);
    await page.waitForFunction(()=>document.getElementById('pending-refs').textContent.includes('2 张参考图'));
    report.steps.push('网页路径接入选中指定 Max 模型，路径参考图和上传参考图均暂存到新任务。');

    await page.click('#create-task');
    await page.waitForFunction(()=>window.__whitebg.task?.reference_images.length===2);
    await page.waitForFunction(()=>window.__whitebg.model,{timeout:30000});
    let task=await page.evaluate(()=>window.__whitebg.task);
    assert.equal(task.preview_source,'export');assert.equal(task.model_max,args.max_file);
    assert.deepEqual(task.reference_images.map(p=>path.basename(p)),['同名.png','同名 (2).png']);
    assert(task.reference_images.every(p=>p.startsWith(args.exchange_dir)&&p.includes(path.sep+'references'+path.sep)));
    report.task_id=task.id;
    report.steps.push('新建任务保留选中的 Max 模型并加载导出预览，两张参考图复制到隔离任务 references 并自动改名。');

    await page.setInputFiles('#reference-input',image);
    await page.waitForFunction(()=>window.__whitebg.task.reference_images.length===3);
    await page.evaluate(reference=>window.whitebgDesktopDrop({product:null,images:[reference],ignored:0,missing:0}),args.reference);
    await page.waitForFunction(()=>window.__whitebg.task.reference_images.length===4);
    await page.evaluate(encoded=>{
      const content=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));
      const transfer=new DataTransfer();transfer.items.add(new File([content],'同名.png',{type:'image/png'}));
      document.body.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer}));
    },image.buffer.toString('base64'));
    await page.waitForFunction(()=>window.__whitebg.task.reference_images.length===5);
    await page.waitForFunction(()=>[...document.querySelectorAll('#reference-list img')].every(img=>img.complete&&img.naturalWidth>0));
    task=await page.evaluate(()=>window.__whitebg.task);
    assert.equal(new Set(task.reference_images).size,5);
    assert.equal(await page.locator('#reference-list [data-ref]').count(),5);
    report.steps.push('任务页内容上传、路径复制和浏览器图片 drop 处理均更新列表；五张同名参考图全部保留并可显示。');

    await page.evaluate(()=>window.whitebgDesktopDrop({product:null,images:[],ignored:2,missing:1}));
    await page.waitForFunction(()=>document.getElementById('toast').textContent.includes('没有拿到这个文件的位置，请用「选择产品文件夹」按钮'));
    assert.match(await page.locator('#toast').textContent(),/已忽略其余 2 项/);
    await page.evaluate(()=>window.__whitebg.flush());
    await page.click('#go-home');
    await page.locator('#start-page').waitFor({state:'visible'});
    assert.equal(await page.locator('#pending-refs').textContent(),'');
    report.steps.push('缺失路径和忽略项均显示提示；已加入任务的暂存参考图不会留在首页。');
    assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.console_errors,[]);assert.deepEqual(report.external_requests,[]);
  }finally{await browser.close();}
}
run().then(()=>{console.log(JSON.stringify(report,null,2));}).catch(error=>{report.failure=String(error.stack||error);console.error(JSON.stringify(report,null,2));process.exitCode=1;});
