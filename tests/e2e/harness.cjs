const fs=require('node:fs');
const path=require('node:path');
const {spawn,execFileSync}=require('node:child_process');
const {setTimeout:sleep}=require('node:timers/promises');
const {chromium}=require(process.env.PLAYWRIGHT_CORE||'playwright-core');
const base=__dirname,repo=path.resolve(base,'..','..');
const outDir=path.join(repo,'tests','tmp','e2e'),root=path.join(outDir,'studio');
const port=8796,url=`http://127.0.0.1:${port}/`;
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const env={...process.env,PYTHONUTF8:'1',PYTHONDONTWRITEBYTECODE:'1'};
const runTag=Date.now();
function prepare(label,largest=false){
  return {...JSON.parse(execFileSync('python.exe',['-B',path.join(base,'environment.py'),'prepare',`${runTag}-${label}`,...(largest?['largest']:[])],{env,encoding:'utf8',windowsHide:true})),
    root,url,exchange_dir:path.join(root,'exchange')};
}
function child(mode){
  fs.mkdirSync(outDir,{recursive:true});
  const fd=fs.openSync(path.join(outDir,`${mode}-${runTag}.log`),'a');
  const proc=spawn('python.exe',['-B','-u',path.join(base,'environment.py'),mode],{cwd:root,env,windowsHide:true,stdio:['pipe',fd,fd]});
  fs.closeSync(fd);
  const closed=new Promise(resolve=>proc.once('close',resolve));
  proc.stdin.on('error',()=>{});
  const stop=async()=>{
    proc.stdin.end('stop\n');
    await Promise.race([closed,sleep(3000)]);
    if(proc.exitCode===null)proc.kill();
    await closed;
  };
  return {proc,closed,stop,once:(partial=false)=>proc.stdin.write(partial?'partial\n':'once\n')};
}
async function request(pathname,options){
  const response=await fetch(url+pathname,options);
  if(!response.ok)throw Error(`接口 ${pathname} 返回 ${response.status}`);
  return response.json();
}
async function until(fn,description,timeout=35000){
  const deadline=Date.now()+timeout;
  while(Date.now()<deadline){
    if(await fn())return;
    await sleep(150);
  }
  throw Error('等待超时：'+description);
}
let runner;
async function withService(run){
  const occupied=await new Promise(resolve=>{
    const connection=require('node:net').connect({port,host:'127.0.0.1'});
    connection.once('error',()=>resolve(false));connection.once('connect',()=>{connection.destroy();resolve(true);});
  });
  if(occupied)throw Error('隔离测试端口已被使用');
  const service=child('serve');
  try{
    await until(()=>request('api/health').then(()=>true,()=>false),'隔离服务');
    return await run({
      async toggleRunner(on,watch=false){
        if(on&&!runner){runner=child(watch?'watch':'runner');await until(async()=>(await request('api/runner')).online,'假执行器心跳');}
        if(!on&&runner){await runner.stop();runner=null;}
      },
      advance(partial=false){runner.once(partial);}
    });
  }finally{
    if(runner){await runner.stop();runner=null;}
    await service.stop();
  }
}
async function withPage(use,viewport={width:1600,height:1000}){
  const browser=await chromium.launch({executablePath:edge,headless:true,env,
    args:['--enable-unsafe-swiftshader','--disable-background-networking','--disable-component-update','--disable-crash-reporter','--no-first-run']});
  try{
    const context=await browser.newContext({viewport,deviceScaleFactor:1});
    const page=await context.newPage(),cdp=await context.newCDPSession(page);
    page.setDefaultTimeout(30000);
    return await use(page,cdp,context,browser);
  }finally{await browser.close();}
}
const modelReady=page=>page.waitForFunction(()=>window.__whitebg?.model&&document.getElementById('loading-model').hidden
  &&document.getElementById('canvas').width>300);
async function scan(page,demo){
  await page.goto(url);await page.waitForFunction(()=>!!window.__whitebg);
  await page.locator('#product-path').fill(demo.product_dir);
  await page.locator('#scan-form button[type=submit]').click();
  await page.locator('#scan-results').waitFor();
}
const done=(page,mode)=>page.waitForFunction(key=>window.__whitebg?.task.items.every(i=>!i.enabled||i[key].state==='done'),mode);
const readyImages=(page,selector)=>page.waitForFunction(selector=>[...document.querySelectorAll(selector)].every(img=>img.complete&&img.naturalWidth>0),selector);
module.exports={base,repo,outDir,root,url,env,edge,runTag,prepare,withService,withPage,request,until,modelReady,scan,done,readyImages};
