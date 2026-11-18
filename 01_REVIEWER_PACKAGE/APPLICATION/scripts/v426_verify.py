"""Reproducible baseline/final runs in fresh copies and fresh PostgreSQL databases."""
from pathlib import Path
import argparse, datetime, hashlib, json, os, shutil, subprocess, sys
import xml.etree.ElementTree as ET
import psycopg2
from psycopg2 import sql

ROOT = Path(__file__).resolve().parents[1]
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
OUT = ROOT / 'reports/v426'
PRIVATE = ROOT.parent / '_CODEX_V426'

def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def manifest(root):
    result = {}
    for directory in ('backend', 'frontend', 'scripts', 'sql', 'schemas', 'workflows'):
        for path in (root / directory).rglob('*'):
            if not path.is_file() or any(x in path.parts for x in ('.venv', 'node_modules', '__pycache__', 'dist', 'data', '.pytest_cache')):
                continue
            if path.suffix.lower() in ('.py', '.ts', '.vue', '.mjs', '.sql', '.yaml', '.json', '.toml', '.txt', '.ps1', '.cmd', '.css', '.html') and not path.name.endswith(('.lock', '.pg-dsn')) and '.private.' not in path.name:
                result[path.relative_to(root).as_posix()] = digest(path)
    return result

def run(phase, reuse=False):
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y%m%dT%H%M%S%f')
    isolated = Path(json.loads((OUT / 'BASELINE_CAPTURED.json').read_text(encoding='utf-8'))['isolated_copy']) if reuse and phase == 'baseline' else PRIVATE / (phase + '_' + stamp)
    if not reuse:
        isolated.mkdir(parents=True)
        for directory in ('backend', 'frontend', 'scripts', 'sql', 'schemas', 'workflows', 'docs'):
            shutil.copytree(ROOT / directory, isolated / directory, ignore=shutil.ignore_patterns('.venv', 'node_modules', 'dist', '__pycache__', '.pytest_cache', '*.sqlite3*', '*.db', '*.pg-dsn', '.env*', '*.private.*', 'v424-runtime', 'v425-runtime', 'v426-runtime'))
        for directory in ('reports/v424', 'reports/v425', 'reports/v426'):
            destination = isolated / directory
            destination.mkdir(parents=True, exist_ok=True)
            for path in (ROOT / directory).glob('*.json'):
                shutil.copy2(path, destination / path.name)
        for path in ROOT.iterdir():
            if path.is_file() and path.suffix in ('.md', '.json', '.cmd'):
                shutil.copy2(path, isolated / path.name)
    source = manifest(isolated)
    (OUT / (phase.upper() + '_SOURCE_SHA256.json')).write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding='utf-8')
    if phase == 'baseline':
        (OUT / 'BASELINE_CAPTURED.json').write_text(json.dumps({'isolated_copy': str(isolated), 'captured_at': stamp, 'source_files': len(source)}, indent=2), encoding='utf-8')
    dsn_file = ROOT.parent / '_CODEX_V424/sample.pg-dsn'
    admin_dsn = dsn_file.read_text(encoding='utf-8').strip()
    params = psycopg2.extensions.parse_dsn(admin_dsn)
    name = ('txbuddy_v425_test_v426_' + phase + '_' + stamp).lower()
    conn = psycopg2.connect(admin_dsn)
    conn.autocommit = True
    with conn.cursor() as cursor:
        cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    conn.close()
    dsn = psycopg2.extensions.make_dsn(**{**params, 'dbname': name})
    env = os.environ.copy()
    env.update(TXBUDDY_RUNTIME_ROOT=str(isolated / 'runtime'), TXBUDDY_CORPUS_ROOT=str(ROOT.parent / 'REAL_DATA_CORPUS/HN_L1_V1_20261116'), TXBUDDY_PG_DSN=dsn, TX_PG_DSN=dsn, TX_PG_PSQL=r'F:\ZHU-FISH\PG_LOCAL\pgsql\bin\psql.exe', PYTHONIOENCODING='utf-8', PYTHONPATH=os.pathsep.join((str(isolated / 'backend'), str(ROOT.parent / '_CODEX_V425/vendor'))))
    for key in ('TXBUDDY_SAMPLE_DSN', 'TXBUDDY_STORE_PATH'):
        env.pop(key, None)
    log = OUT / (phase.upper() + '_BACKEND.log')
    for path in (log, log.with_suffix('.xml'), OUT / (phase.upper() + '_RESULT.json')):
        if path.exists():
            shutil.copy2(path, path.with_name(path.stem + '_attempt_' + stamp + path.suffix))
    command = [sys.executable, '-m', 'pytest', '-q', '--tb=short', '--junitxml=' + str(log.with_suffix('.xml'))]
    if phase == 'baseline':
        command += ['--ignore-glob=tests/test_v426*']
    with log.open('w', encoding='utf-8') as stream:
        completed = subprocess.run(command, cwd=isolated / 'backend', env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=2400)
    suites = list(ET.parse(log.with_suffix('.xml')).getroot().iter('testsuite'))
    totals = {key: sum(int(s.get(key, 0)) for s in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
    result = dict(phase=phase, exit_code=completed.returncode, **totals, log=str(log), isolated_copy=str(isolated), isolated_postgresql_database=name, tested_source_sha256=source, source_unchanged_during_run=(source == manifest(isolated)), working_source_matches_tested_copy=(source == manifest(ROOT)), production_accepted=False, accuracy_E='BLOCKED')
    (OUT / (phase.upper() + '_RESULT.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'tested_source_sha256'}, ensure_ascii=False), flush=True)
    print(log.read_text(encoding='utf-8')[-8000:], flush=True)
    return completed.returncode

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('baseline', 'final'), required=True)
    parser.add_argument('--reuse', action='store_true')
    args = parser.parse_args()
    raise SystemExit(run(args.phase, args.reuse))
