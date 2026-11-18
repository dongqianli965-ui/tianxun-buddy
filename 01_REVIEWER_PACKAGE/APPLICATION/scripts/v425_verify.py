"""Full regression in a fresh code copy and fresh PostgreSQL v425_test database."""
from pathlib import Path
import argparse,datetime,hashlib,json,os,shutil,subprocess,sys,xml.etree.ElementTree as ET
import psycopg2
from psycopg2 import sql

ROOT=Path(__file__).resolve().parents[1]

def code_manifest(root):
    result={}
    for directory in ('backend','frontend','sql','schemas','workflows'):
        for p in (root/directory).rglob('*'):
            if not p.is_file() or any(s in p.parts for s in ('.venv','node_modules','__pycache__','dist','data')):continue
            if p.suffix in ('.py','.ts','.vue','.mjs','.sql','.yaml','.json','.toml','.txt') and p.name!='V425_TASK_QUEUE.json':
                result[p.relative_to(root).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
    for name in ('v425_corpus_governance.py','v425_parse_corpus.py','v425_migration_poc.py','v425_ocr_setup.py','v425_e2e_seed.py'):
        p=root/'scripts'/name
        result[p.relative_to(root).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
    return result

def verify(corpus,admin_dsn,psql):
    stamp=datetime.datetime.now().strftime('%Y%m%dT%H%M%S');private=ROOT.parent/'_CODEX_V425'
    isolated=private/('regression_'+stamp);isolated.mkdir(parents=True)
    for directory in ('backend','frontend','scripts','sql','schemas','workflows','docs'):
        shutil.copytree(ROOT/directory,isolated/directory,ignore=shutil.ignore_patterns('.venv','node_modules','dist','__pycache__','.pytest_cache','*.sqlite3*','*.db','.pg-dsn','.env*','v424-runtime','v425-runtime'))
    for directory in ('reports/v424','reports/v425'):
        dest=isolated/directory;dest.mkdir(parents=True,exist_ok=True)
        for p in (ROOT/directory).glob('*.json'):shutil.copy2(p,dest/p.name)
    for p in ROOT.glob('*.md'):shutil.copy2(p,isolated/p.name)
    params=psycopg2.extensions.parse_dsn(admin_dsn);name='txbuddy_v425_test_regression_'+stamp.lower()
    conn=psycopg2.connect(admin_dsn);conn.autocommit=True
    with conn.cursor() as cur:cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    conn.close();dsn=psycopg2.extensions.make_dsn(**{**params,'dbname':name});(isolated/'test.pg-dsn').write_text(dsn,encoding='utf-8')
    env=os.environ.copy();env.update(TXBUDDY_RUNTIME_ROOT=str(isolated/'runtime'),TXBUDDY_CORPUS_ROOT=str(Path(corpus).resolve()),
        TXBUDDY_PG_DSN=dsn,TX_PG_DSN=dsn,TX_PG_PSQL=str(psql),PYTHONIOENCODING='utf-8',
        PYTHONPATH=os.pathsep.join((str(isolated/'backend'),str(private/'vendor'))))
    env.pop('TXBUDDY_SAMPLE_DSN',None);env.pop('TXBUDDY_STORE_PATH',None)
    out=ROOT/'reports/v425';out.mkdir(parents=True,exist_ok=True);log=out/('regression_'+stamp+'.log')
    before=code_manifest(ROOT)
    with log.open('w',encoding='utf-8') as f:
        run=subprocess.run([sys.executable,'-m','pytest','-q','--tb=short','--junitxml='+str(log.with_suffix('.xml'))],cwd=isolated/'backend',env=env,stdout=f,stderr=subprocess.STDOUT,timeout=2400)
    suite=ET.parse(log.with_suffix('.xml')).getroot();suites=list(suite.iter('testsuite'))
    totals={key:sum(int(s.attrib.get(key,0)) for s in suites) for key in ('tests','failures','errors','skipped')}
    takeover=json.loads((private/'ACTIVE_TAKEOVER.json').read_text(encoding='utf-8'))
    original=json.loads((Path(takeover['snapshot'])/'WORKTREE_SHA256.json').read_text(encoding='utf-8'))
    original_tests=[r for r in original if r['path'].startswith('backend/tests/') and r['path'].endswith('.py')]
    preserved=all((ROOT/r['path']).is_file() and hashlib.sha256((ROOT/r['path']).read_bytes()).hexdigest()==r['sha256'] for r in original_tests)
    dev=[r for r in original if r['path']=='backend/data/txbuddy-dev.sqlite3']
    unchanged=all(hashlib.sha256((ROOT/r['path']).read_bytes()).hexdigest()==r['sha256'] for r in dev)
    result={'exit_code':run.returncode,**totals,'log':str(log),'isolated_copy':str(isolated),
        'isolated_postgresql_database':name,'original_development_db_unchanged':unchanged,
        'all_existing_test_files_preserved':preserved,'existing_test_files':len(original_tests),
        'source_code_unchanged_during_regression':before==code_manifest(ROOT),'tested_source_sha256':before,
        'production_accepted':False,'accuracy_E':'BLOCKED'}
    (out/'REGRESSION_RESULT.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='tested_source_sha256'},ensure_ascii=False));print(log.read_text(encoding='utf-8')[-6000:])
    return 0 if run.returncode==0 and not any(totals[k] for k in ('failures','errors','skipped')) and preserved and unchanged and result['source_code_unchanged_during_regression'] else 2

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--corpus',type=Path,default=ROOT.parent/'REAL_DATA_CORPUS/HN_L1_V1_20261116');p.add_argument('--psql',type=Path,default=Path(r'F:\ZHU-FISH\PG_LOCAL\pgsql\bin\psql.exe'));p.add_argument('--admin-dsn-file',type=Path)
    a=p.parse_args();dsn=os.environ.get('TXBUDDY_TEST_ADMIN_DSN') or (a.admin_dsn_file.read_text().strip() if a.admin_dsn_file else None)
    if not dsn:raise SystemExit('请提供仅用于创建新测试库的 --admin-dsn-file 或 TXBUDDY_TEST_ADMIN_DSN')
    raise SystemExit(verify(a.corpus,dsn,a.psql))
