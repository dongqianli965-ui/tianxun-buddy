// Passive capture for the dedicated TEST_ONLY Chrome profile. Never logs headers,
// form bodies, passwords or tokens. Runs only against its private DevTools port.
import {chromium} from '@playwright/test';
import {readFileSync,existsSync,mkdirSync,appendFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {createHash} from 'node:crypto';
const session=resolve(process.argv[2]||'');
if(!session.split(/[\\/]/).at(-1).startsWith('TEST_ONLY-'))throw new Error('TEST_ONLY session required');
const logs=join(session,'logs'),images=join(session,'browser-captures');mkdirSync(images,{recursive:true});
function record(file,row){appendFileSync(join(logs,file),JSON.stringify({at:new Date().toISOString(),...row})+'\n');}
const active=join(session,'chrome-profile','DevToolsActivePort');
for(let i=0;!existsSync(active)&&i<120;i++)await new Promise(r=>setTimeout(r,500));
if(!existsSync(active))throw new Error('Dedicated Chrome DevTools endpoint unavailable');
const port=Number(readFileSync(active,'utf8').split('\n')[0]);
if(!Number.isInteger(port)||port<1||port>65535)throw new Error('Invalid private Chrome port');
const browser=await chromium.connectOverCDP('http://127.0.0.1:'+port);
let sequence=0;const seen=new WeakSet();
function monitor(page){
  if(seen.has(page))return;seen.add(page);let previous='',busy=false;
  const local=()=>{try{return new URL(page.url()).origin==='http://127.0.0.1:5173';}catch{return false;}};
  page.on('pageerror',e=>{if(local())record('browser-errors.private.jsonl',{type:'pageerror',message:e.message});});
  page.on('console',m=>{if(local()&&['warning','error'].includes(m.type()))record('browser-errors.private.jsonl',{type:m.type(),message:m.text()});});
  page.on('response',r=>{const u=new URL(r.url());if(local()&&u.hostname==='127.0.0.1'&&/^\/(v1|auth|health|files|projects)/.test(u.pathname))record('browser-network.private.jsonl',{method:r.request().method(),path:u.pathname,status:r.status()});});
  page.on('requestfailed',r=>{if(local()){const u=new URL(r.url());record('browser-errors.private.jsonl',{type:'requestfailed',path:u.pathname,error:r.failure()?.errorText});}});
  const timer=setInterval(async()=>{
    if(busy||page.isClosed()||!local())return;busy=true;
    try{const bytes=await page.screenshot({fullPage:false,timeout:5000});const hash=createHash('sha256').update(bytes).digest('hex');if(hash!==previous){previous=hash;const name=String(++sequence).padStart(6,'0')+'.png';const {writeFileSync}=await import('node:fs');writeFileSync(join(images,name),bytes);record('browser-captures.private.jsonl',{file:name,sha256:hash});}}
    catch(e){if(!page.isClosed())record('browser-errors.private.jsonl',{type:'capture_error',message:e.message.split('\n')[0]});}finally{busy=false;}
  },5000);page.on('close',()=>clearInterval(timer));
}
for(const context of browser.contexts()){for(const page of context.pages())monitor(page);context.on('page',monitor);}
record('browser-observer.private.jsonl',{status:'RUNNING',interval_seconds:5,scope:'dedicated TEST_ONLY Chrome; localhost workbench only'});
await new Promise(resolve=>{browser.on('disconnected',resolve);process.on('SIGTERM',resolve);process.on('SIGINT',resolve);});
await browser.close().catch(()=>{});
