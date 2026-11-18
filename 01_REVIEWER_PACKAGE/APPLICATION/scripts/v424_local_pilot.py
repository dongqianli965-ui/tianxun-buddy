"""Initialize/seed a private local pilot runtime, or start its API. Credentials stay outside source."""
from pathlib import Path
import argparse,json,os,secrets,sys,hashlib

ROOT=Path(__file__).resolve().parents[1]
PRIVATE=ROOT.parent/'_CODEX_V424/local-pilot'

def initialize():
    PRIVATE.mkdir(parents=True,exist_ok=True);cfgfile=PRIVATE/'config.private.json'
    from txbuddy.security.users import hash_password
    access=PRIVATE/'LOCAL_PILOT_ACCESS.private.txt'
    dsnfile=ROOT.parent/'_CODEX_V424/sample.pg-dsn'
    if cfgfile.exists():
        cfg=json.loads(cfgfile.read_text(encoding='utf-8'))
        if 'TXBUDDY_SAMPLE_DSN' not in cfg:
            cfg['TXBUDDY_SAMPLE_DSN']=os.environ.get('TXBUDDY_SAMPLE_DSN') or dsnfile.read_text().strip()
            cfgfile.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
        if not access.exists():
            password=secrets.token_urlsafe(24);usersfile=Path(cfg['TXBUDDY_USERS_FILE']);users=json.loads(usersfile.read_text(encoding='utf-8'))
            users['pilot']['password_hash']=hash_password(password);usersfile.write_text(json.dumps(users,ensure_ascii=False),encoding='utf-8')
            access.write_text('仅本机样本环境，禁止分发。\n用户名: pilot\n密码: '+password+'\n入口: http://127.0.0.1:4173\n',encoding='utf-8')
        return cfg
    password=secrets.token_urlsafe(24);users=PRIVATE/'users.private.json'
    users.write_text(json.dumps({'pilot':{'password_hash':hash_password(password),'roles':['estimator'],'display_name':'本机样本操作员'}},ensure_ascii=False),encoding='utf-8')
    cfg={'TXBUDDY_RUNTIME_ROOT':str(PRIVATE/'runtime'),'TXBUDDY_USERS_FILE':str(users),
        'TXBUDDY_JWT_SECRET':secrets.token_urlsafe(48),'TX_INTERNAL_TOKEN':secrets.token_urlsafe(48),
        'TXBUDDY_SAMPLE_DSN':os.environ.get('TXBUDDY_SAMPLE_DSN') or dsnfile.read_text().strip()}
    access.write_text('仅本机样本环境，禁止分发。\n用户名: pilot\n密码: '+password+'\n入口: http://127.0.0.1:4173\n',encoding='utf-8')
    dsn=ROOT.parent/'_CODEX_V424/sample.pg-dsn'
    if dsn.exists():cfg['TXBUDDY_SAMPLE_DSN']=dsn.read_text().strip()
    cfgfile.write_text(json.dumps(cfg,indent=2),encoding='utf-8')
    (PRIVATE/'LOCAL_PILOT_ACCESS.private.txt').write_text('仅本机使用，禁止打包或分发。\n地址：http://127.0.0.1:4173\n用户名：pilot\n密码：'+password+'\n',encoding='utf-8')
    return cfg


def seed(corpus):
    state=PRIVATE/'seed_state.json'
    if state.exists():return json.loads(state.read_text(encoding='utf-8'))
    # Use real signed identity. No authentication override or professional signoff is performed.
    from txbuddy.api import app
    from txbuddy.security.tokens import encode_token
    from fastapi.testclient import TestClient
    settings=app.state.security.settings
    token=encode_token(subject='pilot',settings=settings,roles=('estimator',))
    headers={'Authorization':'Bearer '+token}
    basis={'basis_pack_id':'L1-001-SAMPLE-UNLICENSED','region_code':'411323','region_level':'COUNTY','major':'BUILDING',
        'effective_date':'2026-11-16','measurement_standard_version':'GB/T 50854-2024 METADATA_ONLY',
        'pricing_standard_version':'GB/T 50500-2024 METADATA_ONLY','rule_version_scope':'HENAN_2025',
        'precision_rule_version':'P-V1','status':'DRAFT','test_only':True,'sources':[]}
    with TestClient(app) as c:
        r=c.post('/v1/job',headers=headers,json={'project_id':'L1_001_SAMPLE_PILOT','name':'L1_001 西峡县教师宿舍资料导入试点','business_line':'L1','basis_pack':basis})
        r.raise_for_status();job=r.json();jid=job['job_id'];sources=[]
        for suffix in ('土建_工程量清单.xlsx','安装_工程量清单.xlsx'):
            f=next((Path(corpus)/'05_REAL_PROJECTS/L1_BUDGET/L1_001/B_REFERENCE').glob('*'+suffix));raw=f.read_bytes();path=job['project_id']+'/'+f.name
            r=c.put('/files/'+path+'?job_id='+jid,headers={**headers,'X-Content-SHA256':hashlib.sha256(raw).hexdigest()},content=raw);r.raise_for_status()
            body={'job_id':jid,'path':path};p=c.post('/v1/parse',headers=headers,json=body);p.raise_for_status()
            b=c.post('/v1/pilot/boq-import',headers=headers,json=body);b.raise_for_status();sources.append({'source_file':str(f),'sha256':b.json()['sha256'],'rows':b.json()['count']})
        t=c.post('/v1/job/'+jid+'/tasks',headers=headers,json={'node':'IMPORT','payload':{'sample_only':True}}).json()
        r=c.post('/v1/task/'+t['task_id']+'/complete',headers=headers,json={'result':{'sample_only':True,'accuracy_verification':'BLOCKED'}});r.raise_for_status()
        r=c.post('/v1/deliverables/'+jid+'/generate',headers=headers,json={'kind':'sample_report'});r.raise_for_status();manifest=r.json()
        exported=c.get(manifest['download_url'],headers=headers);exported.raise_for_status()
        export=PRIVATE/'L1_001_SAMPLE_RESULT.xlsx';export.write_bytes(exported.content)
        c.post('/auth/logout',headers=headers)
    result={'job_id':jid,'task_id':t['task_id'],'workflow_status':'DONE','sources':sources,'export':str(export),'export_sha256':manifest['file_sha256'],
        'row_count':sum(s['rows'] for s in sources),'data_role':'B_REFERENCE','project_region':'西峡县','zhengzhou_prices_applied_to_project':False,'accuracy_verification':'BLOCKED'}
    state.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--seed',action='store_true');parser.add_argument('--serve',action='store_true');parser.add_argument('--corpus',type=Path,default=ROOT.parent/'REAL_DATA_CORPUS/HN_L1_V1_20261116')
    args=parser.parse_args();os.environ.update(initialize())
    if args.seed:print(json.dumps(seed(args.corpus),ensure_ascii=False))
    if args.serve:
        import uvicorn
        uvicorn.run('txbuddy.api:app',host='127.0.0.1',port=8010)
