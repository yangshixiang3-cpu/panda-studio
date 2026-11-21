const assert=require('node:assert/strict');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_CORE||'playwright-core');
const args=JSON.parse(process.argv[2]);

async function main(){
  const browser=await chromium.launch({
    executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless:true,
    args:['--enable-unsafe-swiftshader','--disable-background-networking','--disable-component-update',
      '--disable-crash-reporter','--no-first-run'],
  });
  try{
    const page=await browser.newPage({viewport:{width:1600,height:1000},deviceScaleFactor:1});
    const external=[],errors=[];
    await page.route('**/*',route=>{
      if(route.request().url().startsWith(args.url))return route.continue();
      external.push(route.request().url());
      return route.abort();
    });
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(args.url);
    await page.click('#ai-settings-entry');
    await page.locator('#ai-settings-save:enabled').waitFor();
    assert.equal(await page.locator('input[name="ai-engine"][value="codex"]').isChecked(),true);
    await page.getByText('API 中转站',{exact:true}).click();
    await page.fill('#ai-base-url',args.relay);
    await page.fill('#ai-key',process.env.WHITEBG_TEST_KEY);
    await page.click('#ai-settings-save');
    await page.locator('#ai-settings-dialog').waitFor({state:'hidden'});
    await page.click('#ai-settings-entry');
    await page.locator('#ai-load-models:enabled').waitFor();
    assert.match(await page.locator('#ai-key').getAttribute('placeholder'),/已保存.*3517/);
    await page.click('#ai-load-models');
    await page.locator('#ai-model-count').waitFor();
    assert.deepEqual(await page.locator('#ai-models option').evaluateAll(items=>items.map(option=>option.value)),
      ['gpt-6.2-sol','a-model','z-model']);
    assert.equal(await page.inputValue('#ai-model'),'gpt-6.2-sol');
    await page.click('#ai-test-connection');
    await page.waitForFunction(()=>document.querySelectorAll('#ai-test-result .passed').length===4);
    assert.equal(await page.locator('#ai-test-result .failed').count(),0);
    await page.locator('#toast').waitFor({state:'hidden'});
    await page.screenshot({path:path.join(args.shots,'01-ai-settings.png')});
    await page.click('#ai-settings-save');
    await page.locator('#ai-settings-dialog').waitFor({state:'hidden'});

    await page.fill('#product-path',args.product);
    await page.click('#scan-form button[type=submit]');
    await page.locator('#scan-results').waitFor();
    await page.click('#create-task');
    await page.waitForFunction(()=>window.__whitebg?.task&&window.__whitebg?.model);
    await page.click('#calibration-history');
    await page.getByText('中转站 · gpt-6.2-sol',{exact:true}).waitFor();
    await page.click('#calibration-settings');
    await page.locator('#ai-settings-save:enabled').waitFor();
    await page.click('#ai-settings-cancel');
    await page.locator('#ai-settings-dialog').waitFor({state:'hidden'});
    await page.click('#start-calibration');
    await page.locator('#dialog').waitFor({state:'hidden'});
    await page.waitForFunction(()=>window.__whitebg?.task.ai_calibration.state==='waiting_user',null,{timeout:60000});
    await page.click('#calibration-history');
    await page.waitForFunction(()=>document.querySelectorAll('.round-usage').length===2);
    await page.waitForFunction(()=>[...document.querySelectorAll('.round-images img')].every(i=>i.complete&&i.naturalWidth>0));
    assert.equal(await page.locator('#calibration-engine').innerText(),'中转站 · gpt-6.2-sol');
    for(const usage of await page.locator('.round-usage').allTextContents()){
      assert.match(usage,/gpt-6\.2-sol · [\d.]+ 秒 · 12\.3k tokens/);
    }
    await page.locator('#toast').waitFor({state:'hidden'});
    await page.screenshot({path:path.join(args.shots,'02-calibration-engine.png')});
    assert.deepEqual(external,[]);
    assert.deepEqual(errors,[]);
    console.log('设置、模型列表、图片探测和两轮校准均已通过。');
  }finally{await browser.close();}
}

main().catch(error=>{console.error(error);process.exitCode=1;});
