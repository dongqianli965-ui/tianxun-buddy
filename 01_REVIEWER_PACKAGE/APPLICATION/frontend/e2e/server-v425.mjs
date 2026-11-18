import {spawn,spawnSync} from 'node:child_process';
import {mkdtempSync,existsSync,mkdirSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {resolve,join,delimiter} from 'node:path';
import {randomBytes} from 'node:crypto';
const root=resolve('..'),fixtureRoot=resolve(root,'../_CODEX_V425/browser-fixtures');mkdirSync(fixtureRoot,{recursive:true});
const runtime=mkdtempSync(join(fixtureRoot,'txbuddy-v425-TEST_ONLY-e2e-'));
const python=process.env.V425_PYTHON||resolve(root,'../_CODEX_V424/venv/Scripts/python.exe');
const vendor=resolve(root,'../_CODEX_V425/vendor');
const env={...process.env,PYTHONPATH:[join(root,'backend'),vendor].join(delimiter),PYTHONIOENCODING:'utf-8',TXBUDDY_RUNTIME_ROOT:runtime,
  TXBUDDY_USERS_FILE:join(runtime,'users.private.json'),TXBUDDY_JWT_SECRET:randomBytes(48).toString('base64url'),TX_INTERNAL_TOKEN:randomBytes(48).toString('base64url'),TXBUDDY_API_TARGET:'http://127.0.0.1:8016'};
// Only disposable test runtime gets edits. Existing OCR assets are read-only inputs.
const setup=spawnSync(python,[join(root,'scripts/v425_e2e_seed.py'),runtime],{cwd:root,env,windowsHide:true,encoding:'utf8'});
if(setup.status!==0){console.error(setup.stderr||setup.stdout);process.exit(1);}
const backend=spawn(python,['-m','uvicorn','txbuddy.api:app','--host','127.0.0.1','--port','8016'],{cwd:join(root,'backend'),env,windowsHide:true,stdio:'inherit'});
const preview=spawn(process.execPath,[resolve('node_modules/vite/bin/vite.js'),'preview','--host','127.0.0.1','--port','4186','--strictPort'],{env,windowsHide:true,stdio:'inherit'});
function stop(){backend.kill();preview.kill();}
process.on('exit',stop);process.on('SIGINT',()=>{stop();process.exit();});process.on('SIGTERM',()=>{stop();process.exit();});
backend.on('exit',code=>{if(code){stop();process.exit(code);}});preview.on('exit',code=>{if(code){stop();process.exit(code);}});
