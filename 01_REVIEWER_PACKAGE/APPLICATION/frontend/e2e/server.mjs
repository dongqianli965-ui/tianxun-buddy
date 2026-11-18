import {spawn,spawnSync} from 'node:child_process';
import {mkdtempSync,existsSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {resolve,join} from 'node:path';
const root=resolve('..');const runtime=mkdtempSync(join(tmpdir(),'txbuddy-v424-e2e-'));
const preferred=resolve(root,'../_CODEX_V424/venv/Scripts/python.exe');
const python=process.env.V424_PYTHON||(existsSync(preferred)?preferred:resolve(root,'backend/.venv/Scripts/python.exe'));
const env={...process.env,PYTHONPATH:resolve(root,'backend'),PYTHONIOENCODING:'utf-8',TXBUDDY_RUNTIME_ROOT:runtime,
  TXBUDDY_USERS_FILE:join(runtime,'users.private.json'),TXBUDDY_JWT_SECRET:'TEST_ONLY_E2E_EPHEMERAL_'+runtime,
  TX_INTERNAL_TOKEN:'TEST_ONLY_E2E_INTERNAL_'+runtime};
const setup=spawnSync(python,['-c',`import os,json;from pathlib import Path;from txbuddy.security.users import hash_password;Path(os.environ['TXBUDDY_USERS_FILE']).write_text(json.dumps({u:{'password_hash':hash_password('E2E-TEST-ONLY-password'),'roles':['estimator']} for u in ('alice','mallory')}))`],{env,windowsHide:true,encoding:'utf8'});
if(setup.status!==0){console.error(setup.stderr);process.exit(1);}
const backend=spawn(python,['-m','uvicorn','txbuddy.api:app','--host','127.0.0.1','--port','8010'],{cwd:join(root,'backend'),env,windowsHide:true,stdio:'inherit'});
const preview=spawn(process.execPath,[resolve('node_modules/vite/bin/vite.js'),'preview','--host','127.0.0.1','--port','4173','--strictPort'],{env,windowsHide:true,stdio:'inherit'});
function stop(){backend.kill();preview.kill();}
process.on('exit',stop);process.on('SIGINT',()=>{stop();process.exit();});process.on('SIGTERM',()=>{stop();process.exit();});
backend.on('exit',code=>{if(code){stop();process.exit(code);}});preview.on('exit',code=>{if(code){stop();process.exit(code);}});
