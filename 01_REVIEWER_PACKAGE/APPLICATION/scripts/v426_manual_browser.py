"""Run real browser checks and bind their receipt to the current source."""
from pathlib import Path
import json, os, shutil, subprocess
import v426_manual_runtime as runtime
from package_v426 import source_manifest

def run():
    root=runtime.ROOT;out=root/'reports/v426_manual';out.mkdir(parents=True,exist_ok=True)
    session=runtime.load_session();state=runtime.read_json(session/'state.private.json')
    assert state['status']=='RUNNING' and state['environment']=='TEST_ONLY'
    stamp=__import__('datetime').datetime.now().strftime('%Y%m%dT%H%M%S%f')
    attempt=out/('attempt_'+stamp);attempt.mkdir()
    for name in ('BROWSER_RESULTS.json','BROWSER_NETWORK.json','BROWSER.log','BROWSER_RECEIPT.json','visuals','browser-artifacts'):
        path=out/name
        if path.exists():shutil.move(str(path),str(attempt/name))
    before=source_manifest(root);started=runtime.now()
    env=runtime.clean_environment({});env['TXBUDDY_MANUAL_SESSION']=str(session)
    with (out/'BROWSER.log').open('w',encoding='utf-8') as log:
        result=subprocess.run(['npm.cmd','run','test:e2e','--','--config','playwright.manual.config.ts'],cwd=root/'frontend',env=env,stdout=log,stderr=subprocess.STDOUT,timeout=1500)
    receipt={'started_at':started,'finished_at':runtime.now(),'session':str(session),'exit_code':result.returncode,'tested_source_sha256':before,'source_unchanged':before==source_manifest(root),'report_sha256':runtime.digest(out/'BROWSER_RESULTS.json'),'environment':'TEST_ONLY'}
    runtime.write_json(out/'BROWSER_RECEIPT.json',receipt)
    # PowerShell may expose a legacy GBK stdout; preserve the run result even
    # when browser output contains Unicode glyphs that GBK cannot encode.
    summary=(out/'BROWSER.log').read_text(encoding='utf-8')[-10000:]
    try:
        print(summary)
    except UnicodeEncodeError:
        print(summary.encode('ascii','backslashreplace').decode('ascii'))
    return result.returncode

if __name__=='__main__':raise SystemExit(run())
