const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {outDir,url,withService,withPage,prepare,scan,modelReady}=require('./harness.cjs');
const report={checks:[],console:[],pageErrors:[]};
async function run(){
  await withService(async service=>{
    await service.toggleRunner(true);
    await withPage(async page=>{
      page.on('pageerror',error=>report.pageErrors.push(error.message));
      page.on('console',msg=>{if(msg.type()==='error')report.console.push(msg.text());});
      await page.route('**/api/runner',route=>route.fulfill({status:404,json:{error:'接口不存在'}}));
      await scan(page,prepare('compat'));
      assert.equal(await page.locator('#start-page .runner-chip').innerText(),'Max 状态未知');
      await page.locator('input[name=model]').evaluateAll(inputs=>inputs.find(input=>input.value.endsWith('.fbx')).click());
      await page.locator('#create-task').click();
      await modelReady(page);
      const id=await page.evaluate(()=>window.__whitebg.task.id),endpoint='**/api/tasks/'+id;
      await page.evaluate(()=>window.__whitebg.refresh());
      const changes=await page.evaluate(async()=>{
        const {Viewer}=await import('/viewer.js');
        const original=Viewer.prototype.render;
        let render=0;Viewer.prototype.render=function(...args){render++;return original.apply(this,args);};
        let count=0;
        const observer=new MutationObserver(records=>{count+=records.length;});
        observer.observe(document.getElementById('task-page'),{subtree:true,childList:true,characterData:true,attributes:true});
        await window.__whitebg.refresh();
        count+=observer.takeRecords().length;
        observer.disconnect();Viewer.prototype.render=original;
        return {render,count};
      });
      assert.deepEqual(changes,{render:0,count:0});
      report.checks.push('相同 signature 的回包不触发画布绘制。');
      let rev=1,runner={online:false};
      await page.route(endpoint,async route=>{
        if(route.request().method()!=='GET')return route.continue();
        const response=await route.fetch(),payload=await response.json();
        delete payload.signature;delete payload.progress;delete payload.runner;
        payload.revision=rev;
        if(rev>1){
          payload.runner=runner;payload.task.items[0].smallshot.state='rendering';
        }
        return route.fulfill({response,json:payload});
      });
      await page.evaluate(()=>window.__whitebg.refresh());
      assert.equal(await page.locator('#task-page .runner-chip').innerText(),'Max 状态未知');
      rev++;
      await page.evaluate(()=>window.__whitebg.refresh());
      assert.equal(await page.locator('#smallshot').innerText(),'出小样 进行中');
      assert.equal(await page.locator('#task-page .runner-chip').innerText(),'Max 未连接');
      runner={online:true};
      await page.evaluate(()=>window.__whitebg.refresh());
      assert.equal(await page.locator('#task-page .runner-chip').innerText(),'Max');
      await page.route(endpoint+'/calibration',route=>{
        assert.deepEqual(route.request().postDataJSON(),{action:'start',user_note:'稍暖'});
        return route.fulfill({status:400,json:{error:'校准操作无效。'}});
      });
      await page.locator('#calibration-history').click();
      await page.locator('#calibration-note').fill('稍暖');
      await page.locator('#start-calibration').click();
      await page.getByRole('button',{name:'需要更新服务端'}).waitFor();
      assert.deepEqual(report.pageErrors,[]);
      report.checks.push('旧接口场景验证 404 状态未知、revision 回退、进度缺字段及 start 返回 400 的提示。');
    });
  });
}
run().catch(error=>{report.failure=error.stack;console.error(error);process.exitCode=1;}).finally(()=>{
  fs.mkdirSync(outDir,{recursive:true});
  fs.writeFileSync(path.join(outDir,'compat.json'),JSON.stringify(report,null,2));
});
