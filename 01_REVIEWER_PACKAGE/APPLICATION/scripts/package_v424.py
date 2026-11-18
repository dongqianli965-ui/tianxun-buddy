"""Build a SAMPLE_PILOT source release from verified results; never bundle private runtime/data."""
from pathlib import Path
import hashlib,json,re,zipfile,datetime,subprocess

ROOT=Path(__file__).resolve().parents[1]
PRIVATE_NAMES={'.pg-dsn','.pgpass','.netrc','.npmrc','.pypirc','credentials.json','service-account.json'}
PRIVATE_SUFFIXES={'.pem','.key','.pfx','.p12','.jks','.keystore','.sqlite3','.db','.dump','.zip'}


def sensitive_name(path):
    p=Path(path)
    return any(x.startswith('.env') or x.lower() in PRIVATE_NAMES or '.private.' in x.lower() or x.lower().endswith('.secret') for x in p.parts) or p.suffix.lower() in PRIVATE_SUFFIXES


def scan_payload(path,data):
    if sensitive_name(path):raise ValueError('SENSITIVE_PATH:'+path)
    text=data.decode('utf-8',errors='ignore')
    if re.search(r'-----BEGIN [A-Z ]*PRIVATE KEY-----',text):raise ValueError('PRIVATE_KEY_CONTENT:'+path)
    for match in re.finditer(r'postgres(?:ql)?://([^\s/@:]+):([^\s/@]+)@',text):
        pair=':'.join(match.groups()).lower()
        if pair!='user:pass' and not any(marker in pair for marker in ('test','dummy','example','placeholder','changeme','sample','fake','redacted')):
            raise ValueError('CREDENTIAL_DSN:'+path)


def gather(root):
    selected={}
    dirs=('backend','frontend','docs','schemas','scripts','sql','workflows')
    excluded={'.venv','node_modules','__pycache__','.pytest_cache','.git','v424-runtime','test-results','playwright-report','playwright-artifacts'}
    for directory in dirs:
        for f in (root/directory).rglob('*'):
            if not f.is_file():continue
            rel=f.relative_to(root)
            if any(x in excluded for x in rel.parts) or sensitive_name(rel):continue
            if rel.parts[:2]==('backend','data') and (len(rel.parts)>3 or f.suffix not in ('.json','.csv','.md')):continue
            if f.suffix.lower() in ('.pyc','.log','.tsbuildinfo'):continue
            selected[rel.as_posix()]=f.read_bytes()
    for f in root.iterdir():
        if f.is_file() and (f.suffix in ('.md','.json','.cmd') or f.name=='.gitignore') and not sensitive_name(f.name):selected[f.name]=f.read_bytes()
    for f in (root/'tasks').glob('V24-*/*'):
        if f.is_file() and f.suffix in ('.md','.json') and not sensitive_name(f.name):selected[f.relative_to(root).as_posix()]=f.read_bytes()
    report=root/'reports/v424'
    latest_xml=None
    if (report/'REGRESSION_RESULT.json').exists():
        latest_xml=Path(json.loads((report/'REGRESSION_RESULT.json').read_text(encoding='utf-8'))['log']).with_suffix('.xml').name
    for f in report.iterdir():
        if f.is_file() and f.suffix in ('.md','.json','.xml','.log') and not sensitive_name(f.name):
            # Keep final compact proofs; preserve diagnostic/history logs on disk outside the release.
            if f.suffix=='.log' and f.name not in ('frontend_build.log','playwright_final.log','security_and_pilot_regression.log','final_api_subset.log'):continue
            if f.suffix=='.xml' and f.name!=latest_xml:continue
            selected[f.relative_to(root).as_posix()]=f.read_bytes()
    for f in (report/'screenshots').glob('*.png'):selected[f.relative_to(root).as_posix()]=f.read_bytes()
    selected['DELIVERY_PROFILE.md']=b'# SAMPLE_PILOT\n\nProduction cost accuracy and professional signoff remain BLOCKED. Licensed quota data and private runtime are not included.\n'
    return selected


def package(root=ROOT):
    root=Path(root).resolve();reports=root/'reports/v424'
    regression=json.loads((reports/'REGRESSION_RESULT.json').read_text(encoding='utf-8'))
    browser=json.loads((reports/'playwright-results.json').read_text(encoding='utf-8'))
    pg=json.loads((reports/'POSTGRESQL_ACCEPTANCE.json').read_text(encoding='utf-8'))
    if regression['exit_code'] or not regression['legacy_assertions_preserved'] or not regression['original_development_db_unchanged']:
        raise ValueError('REGRESSION_GATE_FAILED')
    if browser['stats']['unexpected'] or browser['stats']['expected']<2:raise ValueError('E2E_GATE_FAILED')
    if pg['postgresql_backup_restore']!='PASS' or pg['postgresql_rollback']!='PASS':raise ValueError('PG_GATE_FAILED')
    files=gather(root)
    for rel,data in files.items():scan_payload(rel,data)
    manifest=''.join(hashlib.sha256(data).hexdigest()+'  '+rel+'\n' for rel,data in sorted(files.items()))
    directory=root/'release';directory.mkdir(exist_ok=True)
    name='TianxunBuddy_V4.2.4_SamplePilot_Safe_20261116.zip'
    if (directory/name).exists():name=name.replace('.zip','_'+datetime.datetime.now().strftime('%H%M%S')+'.zip')
    target=directory/name;tmp=directory/(name+'.tmp')
    try:
        with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            for rel,data in sorted(files.items()):z.writestr(rel,data)
            z.writestr('SHA256SUMS',manifest.encode('utf-8'))
        with zipfile.ZipFile(tmp) as z:
            if z.testzip():raise ValueError('ARCHIVE_CRC_FAILED')
            for rel,data in files.items():
                archived=z.read(rel);scan_payload(rel,archived)
                if hashlib.sha256(data).digest()!=hashlib.sha256(archived).digest():raise ValueError('ARCHIVE_HASH_FAILED:'+rel)
        tmp.replace(target)
    except BaseException:
        if tmp.exists():tmp.unlink()
        raise
    sha=hashlib.sha256(target.read_bytes()).hexdigest()
    (directory/'V424_SHA256SUMS.txt').write_text(sha+'  '+name+'\n',encoding='utf-8')
    report={'version':'0.4.4','profile':'SAMPLE_PILOT','production_accepted':False,'archive':str(target),'sha256':sha,'bytes':target.stat().st_size,
        'payload_files':len(files),'archive_entries':len(files)+1,'all_payload_hashes_verified':True,'staged_scan_hard':0,'archive_scan_hard':0,
        'excluded':['private accounts/config/keys','original corpus','database files/dumps','private worktree snapshot','browser authentication traces','old release packages'],
        'regression_log':regression['log'],'browser_tests':browser['stats']['expected'],'generated_at':datetime.datetime.now().isoformat()}
    (directory/'V424_PACKAGE_REPORT.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (directory/'V424_PACKAGE_REPORT.md').write_text('# V4.2.4 安全打包报告\n\n'+f'- 文件：{name}\n- SHA256：`{sha}`\n- 载荷：{len(files)} 文件，逐项校验通过。\n- 暂存/成品敏感扫描 hard=0。\n- 交付范围：SAMPLE_PILOT；正式计算与专业签认未验收。\n- 原件、私有账号、数据库和历史交付包均单独保留，没有打入本源码包。\n',encoding='utf-8')
    return report


if __name__=='__main__':print(json.dumps(package(),ensure_ascii=False,indent=2))
