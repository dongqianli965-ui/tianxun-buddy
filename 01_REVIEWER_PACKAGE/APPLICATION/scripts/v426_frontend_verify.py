"""Build and browser regression from an isolated tested source copy."""
from pathlib import Path
import argparse, datetime, json, os, shutil, subprocess, sys
import psycopg2
from psycopg2 import sql
from v426_verify import ROOT, OUT, PRIVATE, manifest

def run(phase):
    capture = json.loads((OUT / ('BASELINE_CAPTURED.json' if phase == 'baseline' else 'FINAL_RESULT.json')).read_text(encoding='utf-8'))
    isolated = Path(capture['isolated_copy'])
    frontend = isolated / 'frontend'
    # The browser seed script reuses the read-only V4.2.4 OCR fixture cache.
    # Keep it inside the isolated copy so the tested source/fixture set is
    # reproducible and never reads from the live worktree during the run.
    source_ocr = ROOT / 'reports/v424/ocr'
    target_ocr = isolated / 'reports/v424/ocr'
    if source_ocr.exists() and not target_ocr.exists():
        shutil.copytree(source_ocr, target_ocr)
    for name in ('OCR_CORPUS_RESULTS.json',):
        source_result = ROOT / 'reports/v424' / name
        target_result = isolated / 'reports/v424' / name
        if source_result.exists() and not target_result.exists():
            target_result.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_result, target_result)
    if not (frontend / 'node_modules').exists():
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(frontend / 'node_modules'), str(ROOT / 'frontend/node_modules')], check=True, stdout=subprocess.DEVNULL)
    env = os.environ.copy()
    env.update(PYTHONIOENCODING='utf-8', V425_PYTHON=sys.executable, V425_SEED_DB=str(ROOT.parent / '_CODEX_V425/local-pilot/runtime/governance.sqlite3'), TXBUDDY_CORPUS_ROOT=str(ROOT.parent / 'REAL_DATA_CORPUS/HN_L1_V1_20261116'))
    admin = (ROOT.parent / '_CODEX_V424/sample.pg-dsn').read_text(encoding='utf-8').strip()
    params = psycopg2.extensions.parse_dsn(admin)
    stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f')
    name = ('txbuddy_v425_test_v426_browser_' + phase + '_' + stamp).lower()
    conn = psycopg2.connect(admin)
    conn.autocommit = True
    with conn.cursor() as cursor:
        cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    conn.close()
    env['TXBUDDY_SAMPLE_DSN'] = psycopg2.extensions.make_dsn(**{**params, 'dbname': name})
    # Preserve the existing real sample E2E data read-only; mutations use a fresh clone.
    source_dsn = (ROOT.parent / '_CODEX_V425/e2e.pg-dsn').read_text(encoding='utf-8').strip()
    dump = PRIVATE / (name + '.dump')
    for executable, arguments in [('pg_dump.exe', ['-Fc', '--file', str(dump), '--dbname', source_dsn]), ('pg_restore.exe', ['--no-owner', '--no-privileges', '--dbname', env['TXBUDDY_SAMPLE_DSN'], str(dump)])]:
        completed = subprocess.run([str(Path(r'F:\ZHU-FISH\PG_LOCAL\pgsql\bin') / executable), *arguments], capture_output=True, text=True, encoding='utf-8', errors='replace')
        if completed.returncode:
            raise RuntimeError(executable + ' failed; private restore log retained in isolated workspace')
    before = manifest(isolated)
    result = {'phase': phase, 'isolated_copy': str(isolated), 'isolated_postgresql_database': name, 'production_accepted': False}
    for key, command in [('build', ['npm.cmd', 'run', 'build']), ('browser', ['npm.cmd', 'run', 'test:e2e', '--', '--config', 'playwright.v425.config.ts'])]:
        log = OUT / (phase.upper() + '_FRONTEND_' + key.upper() + '.log')
        if log.exists():
            shutil.copy2(log, log.with_name(log.stem + '_attempt_' + stamp + log.suffix))
        with log.open('w', encoding='utf-8') as stream:
            completed = subprocess.run(command, cwd=frontend, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=1200)
        result[key + '_exit_code'] = completed.returncode
        result[key + '_log'] = str(log)
        print(key + ': ' + str(completed.returncode), flush=True)
        print(log.read_text(encoding='utf-8')[-4000:], flush=True)
        if completed.returncode:
            break
    browser_report = isolated / 'reports/v425/playwright-results.json'
    if browser_report.exists():
        destination = OUT / (phase + '_browser')
        destination.mkdir(exist_ok=True)
        for path in (isolated / 'reports/v425').iterdir():
            if path.name.startswith(('playwright-', 'screenshots', 'GOVERNANCE_E2E_')):
                if path.is_dir():
                    if path.name in ('screenshots',):
                        shutil.copytree(path, destination / path.name, dirs_exist_ok=True)
                else:
                    shutil.copy2(path, destination / path.name)
        result['stats'] = json.loads(browser_report.read_text(encoding='utf-8'))['stats']
    result['source_unchanged_during_run'] = before == manifest(isolated)
    result['tested_source_sha256'] = before
    (OUT / (phase.upper() + '_FRONTEND_RESULT.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return max(result.get('build_exit_code', 1), result.get('browser_exit_code', 1))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('baseline', 'final'), required=True)
    raise SystemExit(run(parser.parse_args().phase))
