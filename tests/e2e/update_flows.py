from pathlib import Path
import re

root = Path(__file__).resolve().parents[1]

def edit(name, changes, preserve=True):
    path = root / (name + '_flow.cjs')
    source = path.read_text(encoding='utf-8')
    if preserve:
        source = re.sub(r'^\s*(?:function preserve\(file\)|const preserve=(?:file|f)=>)\{[^\n]*\}\s*\n', '\n', source, flags=re.M)
        source = re.sub(r'preserve\((?:file|taskScreen|startScreen|screen)\);', '', source)
    for old, new in changes:
        assert old in source, (name, old)
        source = source.replace(old, new)
    path.write_text(source, encoding='utf-8')

edit('browser', [
    ("const {execFileSync}=require('node:child_process');\n", ''),
    ("const root=path.resolve(__dirname,'..');", "const args=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));\nconst {root,product_dir,url,run_dir}=args;"),
    ("function fake(){console.log(execFileSync('python',[path.join(root,'tools/fake_runner.py'),'--once'],{encoding:'utf8'}).trim());}\n", ''),
    ("  const demo=JSON.parse(execFileSync('python',[path.join(root,'tools/prepare_demo.py')],{encoding:'utf8'}));\n  report.product_dir=demo.product_dir;", "  report.product_dir=product_dir;"),
    ("'http://127.0.0.1:8770/'", 'url'),
    ("'http://127.0.0.1:8770/api/file?'", "url+'api/file?'"),
    ("demo.product_dir", 'product_dir'),
    ("    await page.waitForFunction(()=>document.querySelector('#model-message').textContent.includes('等待'));\n    fake();await page.waitForFunction(()=>window.__whitebg?.model,{timeout:30000});",
     "    await page.waitForFunction(()=>window.__whitebg?.model,null,{timeout:30000});"),
    ("    await page.click('#choose-fbx');", "    await page.locator('#preview-menu summary').click();await page.click('#choose-fbx');"),
    ("    await page.click('#overlay-on');", "    await page.click('#tab-references');await page.click('#overlay-on');"),
    ("    // 验证调整与开合", "    await page.click('#tab-pose');\n    // 验证调整与开合"),
    (";await page.waitForFunction(()=>window.__whitebg.task.items[0].smallshot.state==='queued');fake();", ";"),
    (";await page.waitForFunction(()=>window.__whitebg.task.items[0].final.state==='queued');fake();", ";"),
    ("'tests/browser-report.json'", "'browser-report.json'"),
    ("json(path.join(root,'browser-report.json')", "json(path.join(run_dir,'browser-report.json')"),
])

edit('calibration', [
    ("    assert.equal(await page.isChecked('#ai-enabled'),true);\n    await page.click('#smallshot');",
     """    await page.click('#smallshot');
    await page.waitForFunction(()=>window.__whitebg.task.items.every(i=>!i.enabled||i.smallshot.state==='done'));
    assert.deepEqual(await page.evaluate(()=>window.__whitebg.task.ai_calibration.rounds),[]);
    await page.click('#calibration-history');
    await page.fill('#calibration-note','木纹稍暖');
    await page.click('#start-calibration');
    await page.locator('#dialog').waitFor({state:'hidden'});
    assert.equal(await page.locator('.workspace').evaluate(el=>el.inert),false);"""),
    ("出小样自动完成第 0 轮分析、第 1 轮调整和第 2 轮已准，进入待确认。", "出小样未启动校准。手动开始后完成三轮，进入待确认。"),
    ("    assert.match(await page.textContent('#accepted-adjustments'),/正式渲染会套用第 2 轮/);",
     "    assert.match(await page.locator('#accepted-dot').getAttribute('title'),/已采用第 2 轮调整/);"),
])

edit('lighting', [
    ("    const originalPreview=await page.locator('#canvas').evaluate(c=>c.toDataURL());\n    await page.locator('#lighting-panel').scrollIntoViewIfNeeded();",
     "    const originalPreview=await page.locator('#canvas').screenshot();\n    await page.click('#tab-lighting');"),
    ("    await page.waitForFunction(()=>document.querySelector('#lighting-save').textContent==='光向示意图已保存');\n", ''),
    ("    assert.equal(report.uploads.filter(r=>r.status===201).length,await page.evaluate(()=>window.__whitebg.task.items.filter(i=>i.enabled).length));\n", ''),
    ("左前光设置已自动保存，网页自动生成并上传各启用项 PNG。", "左前光设置已自动保存。"),
    ("    assert.equal(await page.locator('#canvas').evaluate(c=>c.toDataURL()),originalPreview);",
     "    await page.waitForFunction(()=>document.querySelector('#lighting-disk').classList.contains('template-light'));\n    assert.deepEqual(await page.locator('#canvas').screenshot(),originalPreview);"),
    ("    await page.click('#smallshot');\n    await page.waitForFunction(()=>window.__whitebg.task.ai_calibration.state==='waiting_user',null,{timeout:60000});",
     """    await page.click('#smallshot');
    await page.waitForFunction(()=>window.__whitebg.task.items.every(i=>!i.enabled||i.smallshot.state==='done'));
    assert.equal(report.uploads.filter(r=>r.status===201).length,await page.evaluate(()=>window.__whitebg.task.items.filter(i=>i.enabled).length));
    await page.click('#calibration-history');await page.click('#start-calibration');
    await page.waitForFunction(()=>window.__whitebg.task.ai_calibration.state==='waiting_user',null,{timeout:60000});"""),
])

edit('lightbox', [
    ("  await page.click('#smallshot');await page.waitForFunction(()=>window.__whitebg.task.ai_calibration.state==='waiting_user',null,{timeout:60000});",
     """  await page.click('#smallshot');
  await page.waitForFunction(()=>window.__whitebg.task.items.every(i=>!i.enabled||i.smallshot.state==='done'));
  await page.click('#calibration-history');await page.click('#start-calibration');
  await page.waitForFunction(()=>window.__whitebg.task.ai_calibration.state==='waiting_user',null,{timeout:60000});"""),
    ("  await page.locator('#reference-list [data-ref]').nth(1).click();",
     "  await page.click('#tab-references');await page.locator('#reference-list [data-ref]').nth(1).click();"),
    ("'AI 校准记录'", "'AI 校准'"),
])

edit('input', [
    ("拖入文件夹需要用桌面窗口打开", "浏览器里不能拖文件夹，请用上面的按钮"),
    (" 里已有 1 个文件，入库时同名旧文件会移到 sku\\旧版\\/", " 里已有 1 个文件，入库时同名旧文件会移到 sku\\旧版\\/"),
    ("/sku 里已有 1 个文件，入库时同名旧文件会移到 sku\\旧版\\//", "/已有 1 个文件/"),
    ("已有 sku 文件夹显示数量和归档位置", "已有 sku 文件夹显示文件数"),
    ("    assert.equal(await page.locator('#reference-list button').count(),5);",
     "    assert.equal(await page.locator('#reference-list [data-ref]').count(),5);"),
], preserve=False)
