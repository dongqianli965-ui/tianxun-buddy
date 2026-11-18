"""V425 runtime copy; the V424 sample environment is never switched or rewritten."""
from pathlib import Path
import argparse,json,os,sqlite3,shutil,secrets,sys

ROOT=Path(__file__).resolve().parents[1]
PRIVATE=ROOT.parent/'_CODEX_V425/local-pilot'

def initialize():
    PRIVATE.mkdir(parents=True,exist_ok=True)
    cfgfile=PRIVATE/'config.private.json'
    if cfgfile.exists():
        return json.loads(cfgfile.read_text(encoding='utf-8'))
    old=ROOT.parent/'_CODEX_V424/local-pilot/config.private.json'
    if not old.exists():
        raise SystemExit('请先提供本机私有运行配置；安全源码包不包含用户或连接串')
    cfg=json.loads(old.read_text(encoding='utf-8'));source=Path(cfg['TXBUDDY_RUNTIME_ROOT']);target=PRIVATE/'runtime'
    target.mkdir(exist_ok=True)
    for p in source.rglob('*'):
        if not p.is_file() or p.name.endswith(('-wal','-shm','-journal')):
            continue
        q=target/p.relative_to(source);q.parent.mkdir(parents=True,exist_ok=True)
        if p.suffix in ('.sqlite3','.db'):
            with sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(q) as dst:
                src.backup(dst)
        else:
            shutil.copy2(p,q)
    cfg['TXBUDDY_RUNTIME_ROOT']=str(target)
    cfg['TXBUDDY_JWT_SECRET']=secrets.token_urlsafe(48)
    cfg['TX_INTERNAL_TOKEN']=secrets.token_urlsafe(48)
    cfg['TXBUDDY_CORPUS_ROOT']=str(ROOT.parent/'REAL_DATA_CORPUS/HN_L1_V1_20261116')
    cfgfile.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
    return cfg

def authorize_catalog(cfg, subject):
    from txbuddy.security.runtime import PersistentAcl
    acl=PersistentAcl(Path(cfg['TXBUDDY_RUNTIME_ROOT'])/'auth.sqlite3')
    projects=json.loads((ROOT/'reports/v425/PROJECT_READINESS.json').read_text(encoding='utf-8'))['projects']
    for project in ['CORPUS_GOVERNANCE']+[p['project_id'] for p in projects]:
        if acl.membership(project,subject) is None:
            acl.grant(project,subject,'OWNER',granted_by='V425_LOCAL_SETUP')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--serve',action='store_true');p.add_argument('--seed',action='store_true')
    a=p.parse_args();cfg=initialize();os.environ.update(cfg)
    vendor=ROOT.parent/'_CODEX_V425/vendor'
    if vendor.exists():
        sys.path.insert(0,str(vendor))
    if a.seed:
        from txbuddy.governance.ocr_review import ReviewStore
        from v425_ocr_setup import ingest_existing
        projects=json.loads((ROOT/'reports/v425/PROJECT_READINESS.json').read_text(encoding='utf-8'))
        store=ReviewStore(Path(cfg['TXBUDDY_RUNTIME_ROOT'])/'governance.sqlite3')
        result=ingest_existing(store,cfg['TXBUDDY_CORPUS_ROOT'],ROOT/'reports/v424/OCR_CORPUS_RESULTS.json',ROOT/'reports/v424/ocr',projects['projects'])
        store.import_catalog('projects',projects)
        store.import_catalog('sources',json.loads((ROOT/'reports/v425/SOURCE_STATUS.json').read_text(encoding='utf-8')))
        authorize_catalog(cfg,'pilot')
        (ROOT/'reports/v425/OCR_WORKBENCH_IMPORT.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k!='items'},ensure_ascii=False))
    if a.serve:
        import uvicorn
        uvicorn.run('txbuddy.api:app',host='127.0.0.1',port=8015)
