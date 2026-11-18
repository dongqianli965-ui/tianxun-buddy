"""Source-only DATA_GOVERNANCE_SAMPLE release with explicit unresolved business gates."""
from pathlib import Path, PurePosixPath
import datetime,hashlib,json,re,zipfile
from package_v424 import gather as gather_previous,scan_payload as scan_previous,sensitive_name

ROOT=Path(__file__).resolve().parents[1]

def sha(data):return hashlib.sha256(data).hexdigest()

def scan_payload(path,data,private_values=()):
    pure=PurePosixPath(path)
    if pure.is_absolute() or '..' in pure.parts or '\\' in path or any(':' in p for p in pure.parts):
        raise ValueError('UNSAFE_ARCHIVE_PATH:'+path)
    scan_previous(path,data)
    for value in private_values:
        if value and len(value)>=8 and value.encode('utf-8') in data:
            raise ValueError('PRIVATE_VALUE_FOUND:'+path)
    text=data.decode('utf-8',errors='ignore')
    if re.search(r'postgres(?:ql)?://[^\s]+:[^\s]+@',text) and not any(x in text.lower() for x in ('test','example','dummy','redacted','placeholder','user:pass')):
        raise ValueError('UNREDACTED_DSN:'+path)

def private_values(root):
    values=[]
    for folder in ('_CODEX_V424','_CODEX_V425'):
        cfg=root.parent/folder/'local-pilot/config.private.json'
        if not cfg.exists():continue
        data=json.loads(cfg.read_text(encoding='utf-8'))
        for key in ('TXBUDDY_JWT_SECRET','TX_INTERNAL_TOKEN'):
            if data.get(key):values.append(data[key])
        users=Path(data.get('TXBUDDY_USERS_FILE',''))
        if users.is_file():
            for spec in json.loads(users.read_text(encoding='utf-8')).values():
                if spec.get('password_hash'):values.append(spec['password_hash'])
        access=cfg.parent/'LOCAL_PILOT_ACCESS.private.txt'
        if access.exists():
            for line in access.read_text(encoding='utf-8').splitlines():
                m=re.search(r'密码\s*[:：]\s*(\S+)',line)
                if m:values.append(m.group(1))
    return values

def validate(root):
    out=root/'reports/v425'
    def read(name):return json.loads((out/name).read_text(encoding='utf-8'))
    regression=read('REGRESSION_RESULT.json');browser=read('playwright-results.json');db=read('DATABASE_POC_RESULT.json');integrity=read('DATA_INTEGRITY.json')
    if regression['exit_code'] or any(regression[k] for k in ('failures','errors','skipped')) or regression['tests']<978:
        raise ValueError('REGRESSION_GATE_FAILED')
    for key in ('all_existing_test_files_preserved','original_development_db_unchanged','source_code_unchanged_during_regression'):
        if not regression[key]:raise ValueError('SOURCE_INTEGRITY_GATE_FAILED:'+key)
    for rel,digest in regression['tested_source_sha256'].items():
        if not (root/rel).is_file() or sha((root/rel).read_bytes())!=digest:raise ValueError('UNTESTED_SOURCE_CHANGE:'+rel)
    if browser['stats']['expected']<3 or any(browser['stats'].get(k,0) for k in ('unexpected','skipped','flaky')):
        raise ValueError('BROWSER_GATE_FAILED')
    br=read('BROWSER_RUN.json')
    if br['exit_code'] or not br['frontend_unchanged_during_test']:raise ValueError('BROWSER_SOURCE_DRIFT')
    for rel,digest in br['tested_source_sha256'].items():
        if sha((root/rel).read_bytes())!=digest:raise ValueError('UNTESTED_FRONTEND_CHANGE:'+rel)
    if not db['migration_poc_passed'] or not all(v['passed'] for v in db['checks'].values()) or db['production_switch_ready'] or db['live_connection_changed']:
        raise ValueError('ISOLATED_POC_GATE_FAILED')
    if not integrity['passed'] or integrity['review_events']!=0 or integrity['production_accepted']:
        raise ValueError('DATA_INTEGRITY_GATE_FAILED')
    queue=json.loads((root/'scripts/V425_TASK_QUEUE.json').read_text(encoding='utf-8'))
    for task in queue['tasks']:
        if task['status']=='BLOCKED':
            if task['id'] not in ('V25-09-HUMAN-REVIEW','V25-10-E-ACCURACY','V25-11-CUTOVER'):raise ValueError('UNDECLARED_BLOCKER')
        elif task['status'] not in ('VERIFIED','CHECKPOINTED') or not task.get('acceptance_passed'):
            raise ValueError('INDEPENDENT_ACCEPTANCE_REQUIRED:'+task['id'])
    return regression,browser,db,integrity

def gather(root):
    files=gather_previous(root)
    # Historic browser traces and large raw originals never enter the new source release.
    for rel in list(files):
        if rel.startswith('reports/v424/') and not (rel.endswith('.json') or rel.endswith('.md')):files.pop(rel)
    for p in (root/'tasks').glob('V25-*/*'):
        if p.is_file() and p.suffix in ('.json','.md','.xml','.log') and not sensitive_name(p.name):files[p.relative_to(root).as_posix()]=p.read_bytes()
    for p in (root/'reports/v425').iterdir():
        if p.is_file() and p.suffix in ('.json','.md','.xml') and not sensitive_name(p.name):files[p.relative_to(root).as_posix()]=p.read_bytes()
    for p in (root/'reports/v425/screenshots').glob('*.png'):files[p.relative_to(root).as_posix()]=p.read_bytes()
    matrix=root/'REAL_PROJECT_READINESS_MATRIX_V425.xlsx';files[matrix.name]=matrix.read_bytes()
    files['DELIVERY_PROFILE.md']=('# DATA_GOVERNANCE_SAMPLE V4.2.5\n\nProduction accepted: False. E-level quantity/quota/pricing accuracy: BLOCKED.\n40 original OCR documents remain NEEDS_EXPERT_REVIEW.\nDOC compatibility: 7 parsed, 2 failed. ZIP scans are unreviewed OCR drafts.\nPostgreSQL migration PoC passed in separate databases; the live adapter was not switched.\nPrivate accounts, keys, original corpus, raw OCR assets, databases, backups and authentication traces are excluded.\n').encode('utf-8')
    return files

def write_archive(files,target,secret_values=()):
    target=Path(target)
    if target.exists():raise FileExistsError('Never overwrite an existing release')
    for rel,data in files.items():scan_payload(rel,data,secret_values)
    manifest=''.join(sha(data)+'  '+rel+'\n' for rel,data in sorted(files.items()))
    tmp=target.with_suffix('.zip.tmp')
    if tmp.exists():raise FileExistsError('Staging path already exists')
    try:
        with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            for rel,data in sorted(files.items()):z.writestr(rel,data)
            z.writestr('SHA256SUMS',manifest.encode('utf-8'))
        with zipfile.ZipFile(tmp) as z:
            if z.testzip():raise ValueError('ARCHIVE_CRC_FAILED')
            for rel,data in files.items():
                archived=z.read(rel);scan_payload(rel,archived,secret_values)
                if sha(archived)!=sha(data):raise ValueError('ARCHIVE_SHA_FAILED:'+rel)
        tmp.replace(target)
    except BaseException:
        if tmp.exists():tmp.unlink()
        raise
    return manifest

def package(root=ROOT):
    root=Path(root);reg,browser,db,integrity=validate(root);files=gather(root);values=private_values(root)
    directory=root/'release';directory.mkdir(exist_ok=True);stamp=datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
    target=directory/('TianxunBuddy_V4.2.5_DataGovernance_Safe_'+stamp+'.zip')
    manifest=write_archive(files,target,values);digest=sha(target.read_bytes())
    (directory/'V425_FILE_SHA256SUMS.txt').write_text(manifest,encoding='utf-8')
    (directory/'V425_SHA256SUMS.txt').write_text(digest+'  '+target.name+'\n',encoding='utf-8')
    report={'version':'V4.2.5','profile':'DATA_GOVERNANCE_SAMPLE','archive':str(target),'sha256':digest,'bytes':target.stat().st_size,
      'payload_files':len(files),'archive_entries':len(files)+1,'payload_hashes_verified':True,'sensitive_findings':0,
      'regression_tests':reg['tests'],'browser_tests':browser['stats']['expected'],'migration_poc_passed':db['migration_poc_passed'],
      'production_switch_ready':False,'production_accepted':False,'accuracy_E':'BLOCKED',
      'excluded':['private credentials/config/runtime','original corpus and raw OCR','databases and backups','authentication traces','historic release archives','private worktree snapshot']}
    (directory/'V425_PACKAGE_REPORT.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False));return report

if __name__=='__main__':package()
