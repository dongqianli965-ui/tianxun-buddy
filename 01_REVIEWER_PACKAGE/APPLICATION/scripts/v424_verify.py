"""Reproduce full regression in a separate worktree copy and a newly-created PostgreSQL test DB."""
from pathlib import Path
import argparse,os,sys,json,hashlib,ast,shutil,subprocess,datetime
import psycopg2
from psycopg2 import sql

ROOT=Path(__file__).resolve().parents[1]

def verify(corpus,admin_dsn,psql):
    stamp=datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
    isolated=ROOT.parent/'_CODEX_V424'/('verify_'+stamp);isolated.mkdir(parents=True)
    for name in ('backend','frontend','docs','scripts','sql','schemas','workflows'):
        shutil.copytree(ROOT/name,isolated/name,ignore=shutil.ignore_patterns('.venv','node_modules','__pycache__','.pytest_cache','*.sqlite3*','.pg-dsn','.env*','v424-runtime'))
    params=psycopg2.extensions.parse_dsn(admin_dsn);name='txbuddy_v424_test_verify_'+stamp.lower()
    conn=psycopg2.connect(admin_dsn);conn.autocommit=True
    with conn.cursor() as cur:cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    conn.close();dsn=psycopg2.extensions.make_dsn(**{**params,'dbname':name})
    env=os.environ.copy();env.update(TXBUDDY_RUNTIME_ROOT=str(isolated/'runtime'),TXBUDDY_CORPUS_ROOT=str(Path(corpus).resolve()),
        TXBUDDY_PG_DSN=dsn,TX_PG_DSN=dsn,TX_PG_PSQL=str(psql),PYTHONIOENCODING='utf-8')
    env.pop('TXBUDDY_SAMPLE_DSN',None);env.pop('PYTHONPATH',None)
    out=ROOT/'reports/v424';out.mkdir(parents=True,exist_ok=True);log=out/('regression_'+stamp+'.log')
    development=ROOT/'backend/data/txbuddy-dev.sqlite3';before=hashlib.sha256(development.read_bytes()).hexdigest() if development.exists() else None
    with log.open('w',encoding='utf-8') as f:
        run=subprocess.run([sys.executable,'-m','pytest','-q','--tb=line','--junitxml='+str(log.with_suffix('.xml'))],cwd=isolated/'backend',env=env,stdout=f,stderr=subprocess.STDOUT)
    baseline=json.loads((out/'LEGACY_ASSERTIONS_BASELINE.json').read_text(encoding='utf-8'))
    checks=[]
    for record in baseline:
        path=ROOT/'backend/tests'/record['file']
        nodes=[ast.dump(n,include_attributes=False) for n in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))) if isinstance(n,ast.Assert)]
        sha=hashlib.sha256(json.dumps(nodes,ensure_ascii=False).encode()).hexdigest()
        checks.append({'file':record['file'],'assertions_preserved':sha==record['assertions_sha256']})
    unchanged=before is None or before==hashlib.sha256(development.read_bytes()).hexdigest()
    result={'exit_code':run.returncode,'log':str(log),'isolated_copy':str(isolated),'isolated_postgresql_database':name,
        'original_development_db_unchanged':unchanged,'legacy_assertions_preserved':all(c['assertions_preserved'] for c in checks),'assertion_checks':checks}
    (out/'REGRESSION_RESULT.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='assertion_checks'},ensure_ascii=False));print(log.read_text(encoding='utf-8')[-4500:])
    return run.returncode if unchanged and result['legacy_assertions_preserved'] else 2


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--corpus',type=Path,default=ROOT.parent/'REAL_DATA_CORPUS/HN_L1_V1_20261116');p.add_argument('--psql',type=Path,default=Path(r'F:\ZHU-FISH\PG_LOCAL\pgsql\bin\psql.exe'))
    args=p.parse_args();dsn=os.environ.get('TXBUDDY_TEST_ADMIN_DSN')
    if not dsn:raise SystemExit('请设置 TXBUDDY_TEST_ADMIN_DSN；仅用于创建全新测试库，不写入该连接的业务表')
    raise SystemExit(verify(args.corpus,dsn,args.psql))
