import {readFile,stat,mkdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {resolve,relative} from 'node:path';

const files=new Map();
async function fileHash(path) {
  const info=await stat(path),signature=[info.dev,info.ino,info.size,info.mtimeMs,info.ctimeMs].join(':');
  if(files.get(String(path))?.signature===signature)return files.get(String(path)).hash;
  const hash=createHash('sha256').update(await readFile(path)).digest('hex');
  if(files.size>=200000)files.clear();
  files.set(String(path),{signature,hash});return hash;
}
async function artifact(request) {
  return new Promise((resolve,reject)=>{
    const child=spawn('python3',[fileURLToPath(new URL('../engine/artifacts.py',import.meta.url))],{stdio:['pipe','pipe','pipe']});
    const output=[],errors=[];
    child.stdout.on('data',value=>output.push(value));child.stderr.on('data',value=>errors.push(value));
    child.on('error',reject);child.on('close',code=>{
      if(code)return reject(Error(Buffer.concat(errors).toString()||'Fixture artifact operation failed'));
      try{resolve(JSON.parse(Buffer.concat(output).toString()));}catch(error){reject(error);}
    });
    child.stdin.on('error',reject);child.stdin.end(JSON.stringify(request));
  });
}
const operational=new Set(['TMPDIR','PWD','OLDPWD','SHLVL','_','PBGATE_PROCESS_OWNER','PBGATE_FIXTURE_OWNER','PBGATE_CONTEXT','PBGATE_PROFILE','PBGATE_SESSION','PBGATE_RECIPE_BINDINGS']);
export async function fixtureTemplate({binary,migrations,hooks,env,password,data,prepare,phase='initial',fresh=false,parameters={},persistent=false,
    directory,artifacts,sessionOnly=false,drivers=[],operationalEnv=[],compilerManifest='.pbgate-dependencies.json',outdir='public',root=process.cwd()}) {
  const persistentRoot=persistent&&!sessionOnly&&artifacts;
  const cache=directory&&(persistentRoot||directory),scope=persistentRoot?'persistent':'session';
  if(!cache||fresh){await prepare();return {hit:false,phase};}
  await mkdir(cache,{recursive:true});
  const identity=async()=>{
    const ignored=new Set([...operational,...operationalEnv]);
    const inventory=source=>artifact({operation:'inventory',cache,source});
    const hookInputs=persistent&&relative(root,resolve(hooks))===outdir?
      await artifact({operation:'preparation',root,outdir,manifest:compilerManifest}):await inventory(hooks);
    return createHash('sha256').update(JSON.stringify({format:4,phase,password,parameters,
      calendar:persistent?new Date().toISOString().slice(0,10):null,
      platform:[process.platform,process.arch,process.versions.bun||process.version],
      implementation:await Promise.all([new URL('./templates.js',import.meta.url),new URL('../engine/artifacts.py',import.meta.url),
        new URL('../engine/runtime.py',import.meta.url),new URL('../engine/dependencies.py',import.meta.url),new URL('./dependencies.js',import.meta.url)].map(fileHash)),
      drivers:await Promise.all(drivers.map(fileHash)),binary:await fileHash(binary),migrations:await inventory(migrations),hooks:hookInputs,
      environment:Object.fromEntries(Object.entries(env).filter(([key])=>!ignored.has(key)).sort(([a],[b])=>a.localeCompare(b)))})).digest('hex');
  };
  const key=await identity();
  const restored=await artifact({operation:'restore',cache,key,destination:data});
  if(restored?.phase===phase)return {hit:true,key,phase,scope};
  await prepare();
  if(await identity()!==key)throw Error('Fixture preparation inputs changed; baseline was not saved');
  await artifact({operation:'save',cache,key,source:data,metadata:{phase}});
  return {hit:false,key,phase,scope};
}
