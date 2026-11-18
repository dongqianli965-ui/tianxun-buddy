"""Run old and new browser suites in a copy so V424 evidence is preserved."""
from pathlib import Path
import datetime,json,os,shutil,subprocess,sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')

ROOT=Path(__file__).resolve().parents[1]

def source_manifest():
    import hashlib
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'frontend').rglob('*') if p.is_file() and p.suffix in ('.ts','.vue','.mjs','.json','.html','.css') and not any(x in p.parts for x in ('node_modules','dist'))}

def run():
    private=ROOT.parent/'_CODEX_V425';stamp=datetime.datetime.now().strftime('%Y%m%dT%H%M%S');before=source_manifest()
    isolated=private/('browser_'+stamp);isolated.mkdir()
    shutil.copytree(ROOT/'frontend',isolated/'frontend',ignore=shutil.ignore_patterns('node_modules','__pycache__'))
    for folder in ('backend','scripts','sql','schemas','workflows'):
        shutil.copytree(ROOT/folder,isolated/folder,ignore=shutil.ignore_patterns('.venv','__pycache__','.pytest_cache','*.sqlite3*','*.db','.pg-dsn','.env*','v424-runtime','v425-runtime'))
    for folder in ('reports/v424','reports/v425'):
        dest=isolated/folder;dest.mkdir(parents=True,exist_ok=True)
        for p in (ROOT/folder).glob('*.json'):shutil.copy2(p,dest/p.name)
    # Existing dependencies and OCR inputs are read-only references. Browser mutations stay in fresh fixtures.
    subprocess.run(['cmd','/c','mklink','/J',str(isolated/'frontend/node_modules'),str(ROOT/'frontend/node_modules')],check=True,stdout=subprocess.DEVNULL)
    env=os.environ.copy();env.update(V425_PYTHON=sys.executable,V425_SEED_DB=str(private/'local-pilot/runtime/governance.sqlite3'),
      TXBUDDY_SAMPLE_DSN=(private/'e2e.pg-dsn').read_text().strip(),
      TXBUDDY_CORPUS_ROOT=str(ROOT.parent/'REAL_DATA_CORPUS/HN_L1_V1_20261116'),
      PYTHONIOENCODING='utf-8')
    out=ROOT/'reports/v425';log=out/('BROWSER_'+stamp+'.log')
    with log.open('w',encoding='utf-8') as f:
        run=subprocess.run(['npm.cmd','run','test:e2e','--','--config','playwright.v425.config.ts'],cwd=isolated/'frontend',env=env,stdout=f,stderr=subprocess.STDOUT,timeout=1200)
    if (isolated/'reports/v425').is_dir():
        for p in (isolated/'reports/v425').iterdir():
            # Only browser outputs may update final evidence. Do not replace generated source/DB audits.
            if p.name.startswith(('playwright-','screenshots','GOVERNANCE_E2E_')):
                q=out/p.name
                if p.is_dir():shutil.copytree(p,q,dirs_exist_ok=True)
                else:shutil.copy2(p,q)
    # Old suites use historic output names. Keep this run under a new V425 namespace.
    legacy=out/('legacy_browser_'+stamp);legacy.mkdir()
    for name in ('E2E_HTTP_EVIDENCE.json','E2E_SAMPLE_RESULT.xlsx'):
        p=isolated/'reports/v424'/name
        if p.exists():shutil.copy2(p,legacy/name)
    result={'exit_code':run.returncode,'isolated_copy':str(isolated),'log':str(log),'legacy_outputs':str(legacy),'production_accepted':False,'frontend_unchanged_during_test':before==source_manifest(),'tested_source_sha256':before}
    (out/'BROWSER_RUN.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False));print(log.read_text(encoding='utf-8')[-4000:]);return run.returncode

if __name__=='__main__':raise SystemExit(run())
