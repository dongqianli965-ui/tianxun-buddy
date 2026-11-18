"""Queue supervisor and one-task independent acceptance workers. No status-only success."""
from pathlib import Path
import argparse,datetime,json,os,subprocess,sys,time,tempfile,msvcrt

ROOT=Path(__file__).resolve().parents[1]
QUEUE=ROOT/'scripts/V425_TASK_QUEUE.json'

def update(task_id,**fields):
    lock=QUEUE.with_suffix('.lock')
    with lock.open('a+b') as f:
        if f.tell()==0:f.write(b'0');f.flush()
        f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_LOCK,1)
        try:
            q=json.loads(QUEUE.read_text(encoding='utf-8'))
            t=next(t for t in q['tasks'] if t['id']==task_id);t.update(fields,updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
            p=QUEUE.with_suffix('.tmp');p.write_text(json.dumps(q,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(p,QUEUE)
        finally:
            f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)

def worker(task_id):
    q=json.loads(QUEUE.read_text(encoding='utf-8'));task=next(t for t in q['tasks'] if t['id']==task_id)
    if task['status']=='BLOCKED':
        print(task_id+': BLOCKED — '+task['reason']);return 3
    out=ROOT/'tasks'/task_id;out.mkdir(exist_ok=True)
    attempt=task.get('attempts',0)+1;stamp=datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
    update(task_id,status='RUNNING',attempts=attempt,worker_pid=os.getpid())
    env=os.environ.copy();env['TXBUDDY_RUNTIME_ROOT']=tempfile.mkdtemp(prefix=task_id+'-',dir=ROOT.parent/'_CODEX_V425')
    env.pop('TXBUDDY_SAMPLE_DSN',None);env['PYTHONIOENCODING']='utf-8'
    log=out/(stamp+'.log');commands=[];exit_code=0
    with log.open('w',encoding='utf-8') as f:
        try:
            if task['tests']:
                argv=[sys.executable,'-m','pytest',*task['tests'],'-q','--tb=short','--junitxml='+str(out/(stamp+'.xml'))]
                result=subprocess.run(argv,cwd=ROOT/'backend',env=env,stdout=f,stderr=subprocess.STDOUT,timeout=1800)
                commands.append({'kind':'independent_pytest','exit_code':result.returncode});exit_code=result.returncode
            if exit_code==0:
                argv=[sys.executable,str(ROOT/'scripts/v425_acceptance.py'),'--task',task['report_check']]
                result=subprocess.run(argv,cwd=ROOT,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=300)
                commands.append({'kind':'independent_report_and_data_checks','exit_code':result.returncode});exit_code=result.returncode
        except (OSError,subprocess.TimeoutExpired) as exc:
            f.write(type(exc).__name__+': acceptance worker did not complete\n');exit_code=124
    result={'task_id':task_id,'worker_pid':os.getpid(),'commands':commands,'exit_code':exit_code,'log':str(log),'production_accepted':False}
    (out/(stamp+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    update(task_id,status=task.get('status_after_acceptance','VERIFIED') if exit_code==0 else 'FAILED',acceptance_passed=exit_code==0,evidence=str(log.relative_to(ROOT)),last_exit_code=exit_code)
    print(json.dumps(result,ensure_ascii=False));return exit_code

def ready(task):
    expected={'baseline':'baseline/REGRESSION_RESULT.json','ocr':'DATA_INTEGRITY.json','sources':'SOURCE_STATUS.json',
      'parsers':'PARSING_RESULTS_OCR.json','matrix':'CATALOG_ACCEPTANCE.json','database':'DATABASE_POC_RESULT.json',
      'integrity':'DATA_INTEGRITY.json','browser':'BROWSER_RUN.json','regression':'REGRESSION_RESULT.json'}
    return (ROOT/'reports/v425'/expected[task['report_check']]).is_file() and all((ROOT/'backend'/p).is_file() for p in task['tests'])

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--worker');p.add_argument('--once',action='store_true');p.add_argument('--poll-seconds',type=int,default=10);p.add_argument('--dsn-file',type=Path)
    a=p.parse_args()
    if a.dsn_file:
        import psycopg2
        dsn=a.dsn_file.read_text().strip()
        if 'v425_test' not in psycopg2.extensions.parse_dsn(dsn).get('dbname',''):
            raise SystemExit('独立 v425_test 数据库连接必需')
        os.environ.update(TXBUDDY_PG_DSN=dsn,TX_PG_DSN=dsn,TX_PG_PSQL=r'F:\ZHU-FISH\PG_LOCAL\pgsql\bin\psql.exe')
    if a.worker:
        raise SystemExit(worker(a.worker))
    failed=False
    while True:
        q=json.loads(QUEUE.read_text(encoding='utf-8'));pending=[t for t in q['tasks'] if t['status'] in ('PENDING','FAILED','CHECKPOINTED') and not t.get('acceptance_passed') and t.get('attempts',0)<3]
        for task in (t for t in pending if ready(t)):
            code=subprocess.run([sys.executable,__file__,'--worker',task['id']],cwd=ROOT).returncode
            failed=failed or code!=0
        if a.once or not pending or (ROOT/'reports/v425/STOP').exists():
            break
        # Each iteration is bounded so the operator can observe/cancel or fix a failed worker.
        time.sleep(max(1,min(30,a.poll_seconds)))
    final=json.loads(QUEUE.read_text(encoding='utf-8'))
    raise SystemExit(1 if any(t['status']=='FAILED' for t in final['tasks']) else 0)
