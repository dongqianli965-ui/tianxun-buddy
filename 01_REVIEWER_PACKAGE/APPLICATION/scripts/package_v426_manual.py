"""Package the tested manual workbench without private runtime data."""
from pathlib import Path
import datetime
import json
import tempfile
import zipfile
from package_v426 import ROOT, gather, private_values, scan_payload, sha, source_manifest, write_archive

REPORT = Path('reports/v426_manual')

def read(root, relative):
    return json.loads((Path(root)/relative).read_text(encoding='utf-8-sig'))

def validate(root=ROOT):
    root=Path(root)
    backend=read(root,REPORT/'regression/FINAL_RESULT.json')
    front=read(root,REPORT/'regression/FINAL_FRONTEND_RESULT.json')
    browser=read(root,REPORT/'BROWSER_RESULTS.json') if (Path(root)/REPORT/'BROWSER_RESULTS.json').exists() else read(root,REPORT/'BROWSER_TESTS_PUBLIC.json')
    summary=read(root,REPORT/'TEST_SUMMARY.json')
    receipt=read(root,REPORT/'BROWSER_RECEIPT.json')
    lifecycle=read(root,REPORT/'RUNTIME_LIFECYCLE.json')
    api=read(root,REPORT/'API_RESULTS.json')
    if not (backend['exit_code']==0 and backend['tests']>=1036 and all(backend[k]==0 for k in ('failures','errors','skipped')) and backend['source_unchanged_during_run'] and backend['working_source_matches_tested_copy']):
        raise ValueError('BACKEND_GATE_FAILED')
    manifest_now=source_manifest(root)
    if any(backend['tested_source_sha256'].get(k)!=v for k,v in manifest_now.items() if k in backend['tested_source_sha256']):
        raise ValueError('SOURCE_CHANGED_SINCE_FINAL_TEST')
    if receipt['exit_code']!=0 or not receipt['source_unchanged'] or any(receipt['tested_source_sha256'].get(k)!=v for k,v in manifest_now.items() if k in receipt['tested_source_sha256']):
        raise ValueError('MANUAL_BROWSER_SOURCE_BINDING_FAILED')
    for rel,expected in lifecycle['launch_source_sha256'].items():
        if sha((root/rel).read_bytes())!=expected:raise ValueError('LAUNCH_SOURCE_CHANGED:'+rel)
    if not(lifecycle['health']['passed'] and lifecycle['stop']['passed'] and lifecycle['health']['original_data_unchanged'] and lifecycle['stop']['original_data_unchanged'] and lifecycle['isolation']['passed'] and lifecycle['isolation']['source_role_write_denied']):
        raise ValueError('RUNTIME_EVIDENCE_FAILED')
    if api['failures']!=0 or api['passed']<6 or any(c['status']!='PASS' for c in api['cases']):
        raise ValueError('API_EVIDENCE_FAILED')
    if not api['source_unchanged'] or any(api['tested_source_sha256'].get(k)!=v for k,v in manifest_now.items() if k in api['tested_source_sha256']) or api['session']!=receipt['session'] or api['requests_sha256']!=sha((root/REPORT/'API_REQUESTS.json').read_bytes()):
        raise ValueError('API_SOURCE_BINDING_FAILED')
    import xml.etree.ElementTree as ET
    suites=list(ET.parse(root/REPORT/'FOCUSED_TESTS.xml').getroot().iter('testsuite'))
    if sum(int(s.get('tests',0)) for s in suites)<21 or any(int(s.get(k,0)) for s in suites for k in ('failures','errors','skipped')):
        raise ValueError('PACKAGING_JUNIT_FAILED')
    if not(front['build_exit_code']==0 and front['browser_exit_code']==0 and front['source_unchanged_during_run'] and front['tested_source_sha256']==backend['tested_source_sha256']):
        raise ValueError('LEGACY_FRONTEND_GATE_FAILED')
    for stats, minimum in ((front['stats'],4),(browser['stats'],6)):
        if stats.get('expected',0)<minimum or any(stats.get(k,1)!=0 for k in ('unexpected','skipped','flaky')):
            raise ValueError('BROWSER_GATE_FAILED')
    if not(summary['production_accepted'] is False and summary['accuracy_E']=='BLOCKED' and summary['source_integrity']=='PASS' and summary['runtime_lifecycle']=='PASS' and summary['packaging_tests_failed']==0 and summary['packaging_tests_passed']>=21 and summary['api_failures']==0):
        raise ValueError('MANUAL_SUMMARY_GATE_FAILED')
    return {'backend_tests':backend['tests'],'legacy_browser_tests':front['stats']['expected'],'manual_browser_tests':browser['stats']['expected']}

def manual_private_values(root=ROOT):
    root=Path(root)
    values=private_values(root)
    from urllib.parse import urlsplit
    parent=root.parent/'_CODEX_V426/manual'
    if parent.exists():
        for file in parent.rglob('*.private.json'):
            try: obj=json.loads(file.read_text(encoding='utf-8-sig'))
            except (ValueError,OSError): continue
            def walk(value,key=''):
                if isinstance(value,dict):
                    for k,v in value.items():walk(v,k)
                elif isinstance(value,list):
                    for v in value:walk(v,key)
                elif isinstance(value,str):
                    if any(k in key.lower() for k in ('password','secret','token')):values.append(value)
                    if 'dsn' in key.lower():
                        values.append(value)
                        try:
                            import psycopg2.extensions
                            password=psycopg2.extensions.parse_dsn(value).get('password')
                            if password:values.append(password)
                        except Exception:pass
            walk(obj)
    return values

def collect(root=ROOT):
    root=Path(root)
    files=gather(root,'V4.2.6 FrontendManual TEST_ONLY')
    for name in ('RUN_V426_FRONTEND_MANUAL.cmd','STOP_V426_FRONTEND_MANUAL.cmd','CHECK_V426_FRONTEND_MANUAL.cmd','FRONTEND_MANUAL_TEST_GUIDE.md'):
        file=root/name
        if not file.is_file():raise ValueError('MISSING_DELIVERY_FILE:'+name)
        files[name]=file.read_bytes()
    for file in (root/REPORT).rglob('*'):
        if not file.is_file():continue
        rel=file.relative_to(root).as_posix()
        # Failed attempts are retained locally. Authentication traces, credentials
        # and private full-page source imagery never enter the public package.
        if any(x in rel for x in ('browser-artifacts','attempt_','final_browser','HANDOFF','BROWSER_RESULTS.json')):continue
        if file.suffix.lower() not in ('.json','.md','.xml','.log','.png','.xlsx'):continue
        if file.suffix.lower()=='.png' and file.parent.name!='visuals':continue
        scan_payload(rel,file.read_bytes())
        files[rel]=file.read_bytes()
    # The JSON reporter contains the full process environment/configuration.
    # Export only its result statistics and individual test statuses.
    raw=read(root,REPORT/'BROWSER_RESULTS.json')
    def cases(suites):
        out=[]
        for suite in suites:
            for spec in suite.get('specs',[]):
                out.append({'title':spec['title'],'status':'PASS' if spec.get('ok') else 'FAIL','tests':[{'status':t.get('status'),'results':[{'status':r.get('status'),'duration':r.get('duration')} for r in t.get('results',[])]} for t in spec.get('tests',[])]})
            out.extend(cases(suite.get('suites',[])))
        return out
    files[(REPORT/'BROWSER_TESTS_PUBLIC.json').as_posix()]=json.dumps({'stats':raw['stats'],'cases':cases(raw.get('suites',[]))},ensure_ascii=False,indent=2).encode()
    return files

def package(root=ROOT):
    root=Path(root).resolve()
    tested=validate(root)
    files=collect(root)
    secret_values=manual_private_values(root)
    stamp=datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f')
    target=root/'release'/('TianxunBuddy_V4.2.6_FrontendManual_'+stamp+'.zip')
    target.parent.mkdir(exist_ok=True)
    manifest=write_archive(files,target,secret_values)
    verify_dir=root.parent/'_CODEX_V426'/('manual_package_verify_'+stamp)
    verify_dir.mkdir(parents=True)
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:raise ValueError('CRC_FAILURE')
        archive.extractall(verify_dir)
    for rel,data in files.items():
        actual=(verify_dir/rel).read_bytes()
        scan_payload(rel,actual,secret_values)
        if sha(actual)!=sha(data):raise ValueError('EXTRACTED_SHA_FAILURE:'+rel)
    report={'status':'PASS','archive':str(target),'sha256':sha(target.read_bytes()),'bytes':target.stat().st_size,'files':len(files),'independent_extraction':str(verify_dir),'manifest_sha256':sha(manifest.encode()),'sensitive_findings':0,'production_accepted':False,'accuracy_E':'BLOCKED',**tested}
    directory=root/'release/v426_manual';directory.mkdir(exist_ok=True)
    (directory/(target.stem+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (directory/(target.stem+'.sha256')).write_text(report['sha256']+'  '+target.name+'\n',encoding='utf-8')
    (root/REPORT/'DELIVERY_SHA256SUMS').write_text(manifest,encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return report

if __name__=='__main__':
    package()
