const fs=require('node:fs'),path=require('node:path');
const {spawn}=require('node:child_process');
const {repo,outDir,env,runTag,prepare,withService}=require('./harness.cjs');
const names=process.argv.slice(2);
async function run(){
  const results=[];
  await withService(async service=>{
    await service.toggleRunner(true,true);
    for(const name of names.length?names:['browser','calibration','input','lightbox','lighting']){
      const run_dir=path.join(outDir,runTag+'-'+name);
      fs.mkdirSync(run_dir,{recursive:true});
      const args=path.join(run_dir,'args.json');
      fs.writeFileSync(args,JSON.stringify({...prepare(name),run_dir}));
      const script=path.join(repo,'tests',name+'_flow.cjs');
      const stdout=fs.openSync(path.join(run_dir,'flow.log'),'w');
      const proc=spawn(process.execPath,[script,args],{env,stdio:['ignore',stdout,stdout],windowsHide:true});
      fs.closeSync(stdout);
      const code=await new Promise(resolve=>proc.once('close',resolve));
      results.push({name,code,run_dir});console.log(name,code,run_dir);
    }
  });
  fs.mkdirSync(outDir,{recursive:true});
  const reportPath=path.join(outDir,'flows.json');
  const prior=names.length&&fs.existsSync(reportPath)?JSON.parse(fs.readFileSync(reportPath,'utf8')):[];
  const byName=new Map([...prior,...results].map(result=>[result.name,result]));
  fs.writeFileSync(reportPath,JSON.stringify([...byName.values()],null,2));
  if(results.some(result=>result.code))process.exitCode=1;
}
run().catch(error=>{console.error(error);process.exitCode=1;});
