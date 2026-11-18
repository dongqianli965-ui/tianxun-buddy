"""Windows manual acceptance runtime. All mutations stay in a TEST_ONLY sandbox.

The launcher never reuses source database credentials in the application process.
Private sessions and evidence live next to, rather than inside, the source tree.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT.parent / '_CODEX_V426' / 'manual'
LATEST = PRIVATE / 'latest.private.json'
CORPUS = ROOT.parent / 'REAL_DATA_CORPUS/HN_L1_V1_20261116'
PG_BIN = ROOT.parent.parent / 'PG_LOCAL/pgsql/bin'
ROLES = ('OWNER', 'EDITOR', 'REVIEWER', 'VIEWER', 'UNAUTHORIZED')
PORTS = {'backend': 8010, 'frontend': 5173}


class Blocked(RuntimeError):
    pass


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def within(path, parent):
    return Path(path).resolve().is_relative_to(Path(parent).resolve())


def assert_session(session):
    session = Path(session).resolve()
    if not within(session, PRIVATE) or not session.name.startswith('TEST_ONLY-'):
        raise Blocked('拒绝非 TEST_ONLY 私有会话目录')
    return session


def port_available(port):
    with socket.socket() as sock:
        if os.name == 'nt':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def require_free_ports():
    busy = [str(port) for port in PORTS.values() if not port_available(port)]
    if busy:
        raise Blocked('端口已占用：' + ', '.join(busy) + '。未终止其他应用；请先自行释放端口或停止本次已有环境。')


def executable(name, candidates=()):
    for candidate in (*candidates, shutil.which(name)):
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise Blocked('缺少运行工具：' + name)


def dependencies():
    for module in ('fastapi', 'uvicorn', 'multipart', 'psycopg2', 'psutil', 'openpyxl', 'docx', 'pypdf', 'bs4', 'charset_normalizer'):
        if importlib.util.find_spec(module) is None:
            raise Blocked('当前 Python 缺少依赖 ' + module + '；请按 backend/requirements.txt 安装到独立虚拟环境。')
    node = executable('node.exe' if os.name == 'nt' else 'node')
    npm = executable('npm.cmd' if os.name == 'nt' else 'npm')
    versions = {}
    for name, command in [('node', [node, '--version']), ('npm', [npm, '--version'])]:
        run = subprocess.run(command, capture_output=True, text=True, timeout=20, creationflags=hidden())
        if run.returncode:
            raise Blocked(name + ' 版本检查失败')
        versions[name] = run.stdout.strip()
    for relative in ('vite/bin/vite.js', '@vitejs/plugin-vue/package.json', 'vue/package.json'):
        if not (ROOT / 'frontend/node_modules' / relative).is_file():
            raise Blocked('前端依赖不完整。请在 frontend 运行 npm ci 后重试。')
    chrome = executable('chrome.exe', [
        Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('LOCALAPPDATA', '')) / 'Google/Chrome/Application/chrome.exe',
    ])
    # Keep executable paths separate from their human-readable versions.  A
    # previous spread of ``versions`` overwrote ``node``/``npm`` with strings
    # such as ``v24.18.0``; Windows then failed to create the Vite process with
    # WinError 2 even though dependency detection had passed.
    return {'python': sys.executable, 'python_version': sys.version.split()[0],
            'node': node, 'node_version': versions['node'],
            'npm': npm, 'npm_version': versions['npm'], 'chrome': chrome}


def hidden():
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def file_manifest(root):
    return {p.relative_to(root).as_posix(): {'bytes': p.stat().st_size, 'sha256': digest(p)}
            for p in sorted(Path(root).rglob('*')) if p.is_file()}


def sqlite_snapshot(path):
    # Back up the committed SQLite view, including WAL pages, through a read-only
    # connection. All scans then use one consistent local view without retaining
    # locks on source stores while hashing rows. Preserve the established logical
    # snapshot format so saved before/after evidence remains directly comparable.
    deadline = time.monotonic() + 120
    def progress(*_):
        if time.monotonic() > deadline:
            raise Blocked('源 SQLite 快照超时：' + str(path))
    with tempfile.TemporaryDirectory(prefix='txbuddy-readonly-snapshot-') as folder:
        snapshot = Path(folder) / 'snapshot.sqlite3'
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as source:
            source.execute('PRAGMA query_only=ON')
            with closing(sqlite3.connect(snapshot)) as conn:
                source.backup(conn, pages=256, progress=progress, sleep=0.05)
                conn.execute('PRAGMA query_only=ON')
                conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
                tables = {}
                for name, schema in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"):
                    quoted = '"' + name.replace('"', '""') + '"'
                    rows = sorted(hashlib.sha256(repr(row).encode()).hexdigest() for row in conn.execute('SELECT * FROM ' + quoted))
                    tables[name] = {'schema': schema, 'rows': len(rows), 'logical_sha256': hashlib.sha256('\n'.join(rows).encode()).hexdigest()}
                integrity = [row[0] for row in conn.execute('PRAGMA integrity_check')]
                foreign_keys = conn.execute('PRAGMA foreign_key_check').fetchall()
                if integrity != ['ok'] or foreign_keys:
                    raise Blocked('源 SQLite 完整性失败：' + str(path))
            return {'tables': tables, 'integrity': integrity, 'foreign_key_violations': foreign_keys, 'file_sha256': digest(path)}


def postgres_snapshot(dsn):
    import psycopg2
    from psycopg2 import sql
    with psycopg2.connect(dsn, connect_timeout=10, options='-c statement_timeout=60000 -c default_transaction_read_only=on') as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor() as cursor:
            cursor.execute('SHOW transaction_read_only')
            if cursor.fetchone()[0] != 'on':
                raise Blocked('无法确认 PostgreSQL 源连接只读')
            cursor.execute("SELECT schemaname, tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1,2")
            tables = cursor.fetchall()
            result = {}
            for schema, table in tables:
                cursor.execute(sql.SQL('SELECT row_to_json(t)::text FROM {}.{} t').format(sql.Identifier(schema), sql.Identifier(table)))
                rows = sorted(hashlib.sha256(row[0].encode()).hexdigest() for row in cursor)
                cursor.execute("SELECT column_name,data_type,udt_name,is_nullable,column_default FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (schema, table))
                # Keep snapshots JSON-native; json round-trips tuples as lists.
                columns = [list(row) for row in cursor.fetchall()]
                result[schema + '.' + table] = {'rows': len(rows), 'sha256': hashlib.sha256('\n'.join(rows).encode()).hexdigest(), 'columns': columns}
            return result


def source_settings():
    cfgfile = ROOT.parent / '_CODEX_V425/local-pilot/config.private.json'
    source = ROOT.parent / '_CODEX_V425/e2e.pg-dsn'
    admin = ROOT.parent / '_CODEX_V424/sample.pg-dsn'
    for required in (cfgfile, source, admin):
        if not required.is_file():
            raise Blocked('缺少本机私有配置：' + str(required) + '；不能确认数据库隔离，停止启动。')
    cfg = read_json(cfgfile)
    return cfg, source.read_text(encoding='utf-8').strip(), admin.read_text(encoding='utf-8').strip()


def original_snapshot(source_dsn):
    cfg, _, _ = source_settings()
    runtime = Path(cfg['TXBUDDY_RUNTIME_ROOT'])
    sqlite = sorted(runtime.glob('*.sqlite3')) + [ROOT / 'backend/data/txbuddy-dev.sqlite3']
    if not CORPUS.is_dir() or not (ROOT / 'reports/v424/ocr').is_dir():
        raise Blocked('真实原件或 OCR 缓存缺失，不能准备人工环境')
    snapshots = {}
    for path in sqlite:
        print('只读完整性检查：' + path.name, flush=True)
        snapshots[str(path.resolve())] = sqlite_snapshot(path)
    print('只读 PostgreSQL 快照检查...', flush=True)
    return {'corpus': file_manifest(CORPUS), 'ocr_assets': file_manifest(ROOT / 'reports/v424/ocr'),
            'sqlite': snapshots, 'postgres': postgres_snapshot(source_dsn)}


def pg_env(dsn):
    import psycopg2.extensions
    params = psycopg2.extensions.parse_dsn(dsn)
    env = {k: v for k, v in os.environ.items() if not k.startswith('PG')}
    for key, value in params.items():
        name = {'dbname': 'PGDATABASE', 'user': 'PGUSER', 'password': 'PGPASSWORD', 'host': 'PGHOST', 'port': 'PGPORT', 'sslmode': 'PGSSLMODE', 'options': 'PGOPTIONS'}.get(key)
        if name:
            env[name] = value
    return env


def clone_postgres(session, source_dsn, admin_dsn):
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extensions import make_dsn, parse_dsn
    suffix = datetime.now().strftime('%Y%m%d%H%M%S') + secrets.token_hex(3)
    database = 'txbuddy_test_only_manual_' + suffix
    role = 'txbuddy_manual_' + suffix
    password = secrets.token_urlsafe(36)
    params = parse_dsn(admin_dsn)
    if database == parse_dsn(source_dsn).get('dbname'):
        raise Blocked('源库与目标库不能相同')
    dump = session / 'source.private.dump'
    dump_cmd = [executable('pg_dump.exe', [PG_BIN / 'pg_dump.exe']), '-Fc', '--no-password', '--file', str(dump)]
    dump_result = subprocess.run(dump_cmd, env={**pg_env(source_dsn), 'PGOPTIONS': '-c default_transaction_read_only=on'}, capture_output=True, timeout=180, creationflags=hidden())
    (session / 'logs/pg_dump.private.log').write_bytes(dump_result.stdout + dump_result.stderr)
    if dump_result.returncode:
        raise Blocked('只读 pg_dump 失败；诊断保存在私有日志')
    conn = psycopg2.connect(admin_dsn, connect_timeout=10)
    conn.autocommit = True
    with conn.cursor() as cursor:
        cursor.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION').format(sql.Identifier(role)), (password,))
        cursor.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(database), sql.Identifier(role)))
        cursor.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(database)))
    conn.close()
    target_admin = make_dsn(**{**params, 'dbname': database})
    restore = subprocess.run([executable('pg_restore.exe', [PG_BIN / 'pg_restore.exe']), '--exit-on-error', '--no-owner', '--no-privileges', '--role', role, '--dbname', database, str(dump)], env=pg_env(target_admin), capture_output=True, timeout=180, creationflags=hidden())
    (session / 'logs/pg_restore.private.log').write_bytes(restore.stdout + restore.stderr)
    if restore.returncode:
        raise Blocked('隔离 pg_restore 失败；未启动服务，诊断保存在私有日志')
    target = make_dsn(**{**params, 'dbname': database, 'user': role, 'password': password})
    verify_clone_identity(target, database, source_dsn)
    # Ensure the restored database is a complete read-only clone before use.
    if postgres_snapshot(source_dsn) != postgres_snapshot(target):
        raise Blocked('隔离 PostgreSQL 克隆与源只读快照不一致，停止启动')
    source_params = parse_dsn(source_dsn)
    source_as_test_role = make_dsn(**{**source_params, 'user': role, 'password': password})
    # PostgreSQL has no DENY grant; prove this new role has no persistent write
    # privileges in the source database instead of changing source privileges.
    try:
        with psycopg2.connect(source_as_test_role, connect_timeout=10) as read_source:
            read_source.set_session(readonly=True)
            with read_source.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind IN ('r','p','v','m','f') AND (has_table_privilege(c.oid,'INSERT') OR has_table_privilege(c.oid,'UPDATE') OR has_table_privilege(c.oid,'DELETE') OR has_table_privilege(c.oid,'TRUNCATE'))")
                if cursor.fetchone()[0]:
                    raise Blocked('隔离账号仍有源库表写权限，停止启动')
                cursor.execute("SELECT count(*) FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema' AND has_schema_privilege(oid,'CREATE')")
                if cursor.fetchone()[0]:
                    raise Blocked('隔离账号仍有源库 schema 创建权限，停止启动')
    except psycopg2.OperationalError as exc:
        # Connection denied is stronger than lack of table write privileges.
        if 'permission denied for database' not in str(exc):
            raise Blocked('无法证明隔离账号对源库无写权限') from None
    return {'database': database, 'role': role, 'dsn': target, 'source_role_write_denied': True, 'clone_method': 'read-only pg_dump / pg_restore'}


def verify_clone_identity(dsn, expected_database, source_dsn):
    import psycopg2
    from psycopg2.extensions import parse_dsn
    params = parse_dsn(dsn)
    if not expected_database.startswith('txbuddy_test_only_manual_') or params.get('dbname') != expected_database or params.get('dbname') == parse_dsn(source_dsn).get('dbname'):
        raise Blocked('PostgreSQL 测试库隔离断言失败')
    with psycopg2.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cursor:
            cursor.execute('SELECT current_database(), rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user')
            row = cursor.fetchone()
            if not row or row[0] != expected_database or any(row[1:]):
                raise Blocked('PostgreSQL 实际连接或测试账号权限不符合隔离要求')
            cursor.execute('CREATE TEMP TABLE manual_isolation_probe (v integer)')
            cursor.execute('INSERT INTO manual_isolation_probe VALUES (1)')
        conn.rollback()


def clean_environment(config):
    # Never inherit production DSNs, credentials, file roots, or professional registries.
    env = {k: v for k, v in os.environ.items() if not (k.startswith(('TXBUDDY_', 'TX_PG_', 'PG')) or k in ('TX_INTERNAL_TOKEN',))}
    env.update(config)
    env['PYTHONPATH'] = os.pathsep.join((str(ROOT / 'backend'), str(ROOT / 'scripts'), str(ROOT.parent / '_CODEX_V425/vendor')))
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return env


def prepare_fixtures(session, config):
    os.environ.update(config)
    for p in (ROOT / 'backend', ROOT / 'scripts', ROOT.parent / '_CODEX_V425/vendor'):
        sys.path.insert(0, str(p))
    from txbuddy.security.users import hash_password
    from txbuddy.security.runtime import PersistentAcl
    from txbuddy.governance.ocr_review import ReviewStore
    from txbuddy.store import EvidenceStore
    from v425_ocr_setup import ingest_existing
    runtime = session / 'runtime'
    corpus = session / 'source-copies'
    images = session / 'ocr-assets'
    shutil.copytree(CORPUS, corpus)
    shutil.copytree(ROOT / 'reports/v424/ocr', images)
    accounts = {}
    users = {}
    for role in ROLES:
        username = role.lower()
        password = secrets.token_urlsafe(18)
        accounts[role] = {'username': username, 'password': password}
        users[username] = {'password_hash': hash_password(password), 'roles': ['estimator'], 'display_name': 'TEST_ONLY ' + role}
    write_json(session / 'users.private.json', users)
    write_json(session / 'professionals.private.json', {})
    projects = read_json(ROOT / 'reports/v425/PROJECT_READINESS.json')
    sources = read_json(ROOT / 'reports/v425/SOURCE_STATUS.json')
    governance_store = ReviewStore(runtime / 'governance.sqlite3')
    imported = ingest_existing(governance_store, corpus, ROOT / 'reports/v424/OCR_CORPUS_RESULTS.json', images, projects['projects'])
    governance_store.import_catalog('projects', projects)
    governance_store.import_catalog('sources', sources)
    acl = PersistentAcl(runtime / 'auth.sqlite3')
    ids = ['TEST_ONLY-MANUAL-A', 'TEST_ONLY-MANUAL-B', 'CORPUS_GOVERNANCE'] + [p['project_id'] for p in projects['projects']]
    for project in ids:
        for role in ROLES[:-1]:
            acl.grant(project, role.lower(), role, granted_by='TEST_ONLY_MANUAL_FIXTURE')
    with acl.connect() as conn:
        # Explicit synthetic fixture ownership; never repairs an existing real project.
        for project in ('TEST_ONLY-MANUAL-A', 'TEST_ONLY-MANUAL-B'):
            conn.execute('INSERT INTO acl_project VALUES (?,?)', (project, 'owner'))
    # Seed selectable business jobs in the isolated store. These synthetic
    # records make project/job switching testable without changing imported
    # real-project data.
    business_store = EvidenceStore(runtime / 'business.sqlite3')
    basis = {
        'basis_pack_id': 'TEST_ONLY-MANUAL', 'test_only': True,
        'region_code': '410100', 'region_level': 'CITY', 'major': 'BUILDING',
        'effective_date': '2026-01-01',
        'measurement_standard_version': 'GB/T 50854-2024',
        'pricing_standard_version': 'GB/T 50500-2024',
        'rule_version_scope': 'HENAN_2025', 'precision_rule_version': 'P-V1',
        'status': 'DRAFT', 'sources': [],
    }
    for suffix, project in (('A', 'TEST_ONLY-MANUAL-A'), ('B', 'TEST_ONLY-MANUAL-B')):
        business_store.create_job('TEST_ONLY-JOB-' + suffix, project, 'TEST_ONLY 手工验收项目 ' + suffix, 'L1', basis, 'owner')
    fixture = {'accounts': accounts, 'projects': ids, 'ocr_import': {k:v for k,v in imported.items() if k!='items'}, 'human_reviews_created': 0, 'professional_signoffs_created': 0, 'sample_copies': str(corpus), 'boq_folder': str(corpus/'05_REAL_PROJECTS/L1_BUDGET/L1_001/B_REFERENCE'), 'production_accepted': False, 'accuracy_E': 'BLOCKED'}
    write_json(session / 'TEST_ACCOUNTS.private.json', fixture)
    lines = ['# V4.2.6 TEST_ONLY 本机私有测试账号', '', '请勿提交、打包或发送本文件。测试账号没有真实专业签认资格。', '', '| 角色 | 用户名 | 本次随机临时密码 |', '|---|---|---|']
    lines += ['| '+role+' | '+spec['username']+' | '+spec['password']+' |' for role,spec in accounts.items()]
    lines += ['', '工作台：http://127.0.0.1:5173', '项目：TEST_ONLY-MANUAL-A、TEST_ONLY-MANUAL-B；可在工作台新建项目。', '测试任务：TEST_ONLY-JOB-A、TEST_ONLY-JOB-B。', '真实样本只使用本会话副本：'+str(corpus), 'B_REFERENCE：'+fixture['boq_folder'], '新项目 OWNER 应通过系统既有 owner-claim 流程生成；此环境不自动补齐真实项目 OWNER。', '所有自动化操作只属于 TEST_ONLY，不计为真实人工复核。', 'JWT 过期时间 3600 秒。临时密码只在本机私有文件中保存。']
    (session / 'TEST_ACCOUNTS.private.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return fixture


def process_record(process, command, session):
    import psutil
    # A short-lived launcher (for example a failed Vite process) can exit
    # between Popen and metadata collection.  Poll briefly and raise a
    # descriptive Blocked error instead of leaking psutil.NoSuchProcess.
    deadline = time.monotonic() + 3.0
    last_error = None
    while time.monotonic() < deadline:
        try:
            found = psutil.Process(process.pid)
            if not found.is_running():
                raise psutil.NoSuchProcess(process.pid)
            # Windows venv Python launches an actual interpreter child.
            # Persist its identity while the parent is alive so STOP can also
            # handle an orphan.
            time.sleep(.3)
            record = {'pid': process.pid, 'create_time': found.create_time(), 'command': found.cmdline(), 'launched_command':list(command), 'session': str(session), 'executable': found.exe()}
            record['children'] = [identity_record(child) for child in found.children(recursive=True)]
            return record
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError) as exc:
            last_error = exc
            if process.poll() is not None:
                break
            time.sleep(.1)
    detail = f'进程未能保持运行（returncode={process.poll()}）'
    if last_error:
        detail += f': {type(last_error).__name__}'
    raise Blocked(detail + '；详细日志位于本次会话 logs 目录')


def identity_record(process):
    return {'pid': process.pid, 'create_time': process.create_time(),
            'command': process.cmdline(), 'executable': process.exe()}


def matching_process(record, process_factory=None):
    import psutil
    try:
        process = (process_factory or psutil.Process)(int(record['pid']))
        if abs(process.create_time() - float(record['create_time'])) > 0.01:
            return None
        command = process.cmdline()
        if Path(process.exe()).resolve() != Path(record['executable']).resolve():
            return None
        expected = record['command']
        if len(command) != len(expected) or any(a != b for a,b in zip(command[1:],expected[1:])):
            return None
        return process
    except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, ValueError):
        return None


def stop_owned(record):
    import psutil
    process = matching_process(record)
    records = list(record.get('children', []))
    if process is not None:
        # Only descendants of a fully identity-verified owner are captured.
        records.extend(identity_record(child) for child in process.children(recursive=True))
        records.append(record)
    seen = set()
    stopped = []
    for owned in reversed(records):
        if owned['pid'] in seen:
            continue
        seen.add(owned['pid'])
        child = matching_process(owned)
        if child is None:
            continue
        try:
            child.terminate()
            child.wait(timeout=10)
        except psutil.TimeoutExpired:
            if matching_process(owned) is not None:
                child.kill()
                try:
                    child.wait(timeout=3)
                except psutil.TimeoutExpired:
                    # Windows can retain a terminated browser process briefly
                    # while descendants and crash handlers unwind. Continue
                    # recording the verified PID instead of aborting cleanup.
                    pass
        stopped.append(owned['pid'])
    remaining = [r['pid'] for r in records if matching_process(r) is not None]
    return {'pid': record['pid'], 'stopped': not remaining, 'identity_verified': True, 'stopped_pids': stopped, 'remaining_pids': remaining}


def wait_url(url, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    raw=response.read()
                    return json.loads(raw) if 'json' in response.headers.get('Content-Type','') else {'status':'ok'}
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
            time.sleep(.5)
    raise Blocked('启动健康检查超时：' + url)


def load_session(argument=None):
    if argument:
        return assert_session(argument)
    if not LATEST.is_file():
        raise Blocked('尚无本轮 TEST_ONLY 会话，请先运行 RUN_V426_FRONTEND_MANUAL.cmd')
    return assert_session(read_json(LATEST)['session'])


def start(no_browser=False):
    PRIVATE.mkdir(parents=True, exist_ok=True)
    if LATEST.is_file():
        old = load_session()
        old_state = read_json(old / 'state.private.json')
        running = [r for r in old_state.get('processes',{}).values() if matching_process(r)]
        if running:
            print('已有本轮环境，未重复启动。')
            return check(old)
    require_free_ports()
    deps = dependencies()
    session = PRIVATE / ('TEST_ONLY-' + datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(3))
    session.mkdir()
    (session / 'logs').mkdir()
    (session / 'runtime').mkdir()
    state = {'session': str(session), 'created_at': now(), 'environment': 'TEST_ONLY', 'version': 'V4.2.6', 'status': 'PREPARING', 'processes': {}, 'production_accepted': False, 'accuracy_E': 'BLOCKED', 'dependencies': deps}
    write_json(session/'state.private.json', state)
    write_json(LATEST, {'session': str(session)})
    try:
        _, source_dsn, admin_dsn = source_settings()
        # Fail early with a private diagnostic when the source service is down,
        # before expensive file hashing. Never start or mutate a source cluster.
        import psycopg2
        with psycopg2.connect(source_dsn, connect_timeout=10, options='-c default_transaction_read_only=on') as probe:
            with probe.cursor() as cursor:
                cursor.execute('SHOW transaction_read_only')
                if cursor.fetchone()[0] != 'on':
                    raise Blocked('源 PostgreSQL 无法建立只读连接')
        print('核对原始资料、SQLite 与 PostgreSQL 只读快照...', flush=True)
        before = original_snapshot(source_dsn)
        write_json(session/'ORIGINAL_BEFORE.private.json', before)
        print('使用 pg_dump/pg_restore 创建独立 TEST_ONLY 数据库和最小权限账号...', flush=True)
        pg = clone_postgres(session, source_dsn, admin_dsn)
        state['postgres'] = {k:v for k,v in pg.items() if k != 'dsn'}
        # Persist the clone identity before launching the backend.  The
        # backend validates this state during its own startup; without an
        # early write it could race ahead and observe the initial PREPARING
        # state without the postgres block, then fail with KeyError.
        write_json(session/'state.private.json', state)
        config = {'TXBUDDY_RUNTIME_ROOT': str(session/'runtime'), 'TXBUDDY_STORE_PATH': str(session/'runtime/business.sqlite3'), 'TXBUDDY_USERS_FILE': str(session/'users.private.json'), 'TXBUDDY_PROFESSIONALS_FILE': str(session/'professionals.private.json'), 'TXBUDDY_FILE_ROOT': str(session/'runtime/files'), 'TXBUDDY_DERIVED_ROOT': str(session/'runtime/derived'), 'TXBUDDY_CORPUS_ROOT': str(session/'source-copies'), 'TXBUDDY_JWT_SECRET': secrets.token_urlsafe(48), 'TX_INTERNAL_TOKEN': secrets.token_urlsafe(48), 'TXBUDDY_TEST_AUTOLOGIN_TOKEN': secrets.token_urlsafe(32), 'TXBUDDY_JWT_TTL': '3600', 'TXBUDDY_SAMPLE_DSN': pg['dsn'], 'TXBUDDY_PG_DSN': pg['dsn'], 'TX_PG_DSN': pg['dsn'], 'TXBUDDY_SQL_DIR': str(ROOT/'sql'), 'TXBUDDY_API_TARGET': 'http://127.0.0.1:8010', 'TXBUDDY_ENV': 'TEST_ONLY', 'VITE_TXBUDDY_ENV': 'TEST_ONLY', 'VITE_TXBUDDY_VERSION': 'V4.2.6'}
        write_json(session/'config.private.json', config)
        print('复制真实资料和 OCR 页图到会话，建立五种临时身份（不授予专业资质）...', flush=True)
        fixture = prepare_fixtures(session, config)
        after = original_snapshot(source_dsn)
        if before != after:
            raise Blocked('启动准备前后原始数据快照不一致，拒绝启动')
        write_json(session/'ISOLATION_BEFORE_START.json', {'passed': True, 'corpus_files': len(before['corpus']), 'sqlite_databases': len(before['sqlite']), 'source_pg_readonly_snapshot_unchanged': True, 'source_role_write_denied': True, 'test_database': pg['database'], 'human_reviews_created': 0, 'professional_signoffs_created': 0, 'production_accepted': False, 'accuracy_E': 'BLOCKED'})
        env = clean_environment(config)
        commands = {'backend': [sys.executable, str(Path(__file__).resolve()), 'serve-backend', '--session', str(session)], 'frontend': [deps['node'], str(ROOT/'frontend/node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', '5173', '--strictPort']}
        require_free_ports()
        for name, command in commands.items():
            with (session / 'logs' / (name + '.log')).open('ab') as stream:
                process = subprocess.Popen(command, cwd=ROOT/'frontend' if name=='frontend' else ROOT, env=env, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, creationflags=hidden())
            state['processes'][name] = process_record(process, command, session)
            write_json(session/'state.private.json', state)
        health = wait_url('http://127.0.0.1:8010/health')
        if health.get('status') != 'ok' or health.get('environment') != 'TEST_ONLY' or health.get('test_only') is not True:
            raise Blocked('后端 /health 未返回正常状态')
        wait_url('http://127.0.0.1:5173')
        state['status'] = 'RUNNING'
        state['started_at'] = now()
        state['frontend_url'] = 'http://127.0.0.1:5173'
        state['backend_url'] = 'http://127.0.0.1:8010'
        write_json(session/'state.private.json', state)
        if not no_browser:
            chrome_url=state['frontend_url']+'/?test_only_autologin=owner&bootstrap='+config['TXBUDDY_TEST_AUTOLOGIN_TOKEN']
            chrome_command=[deps['chrome'], '--user-data-dir='+str(session/'chrome-profile'), '--no-first-run', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', '--new-window', chrome_url]
            chrome_process=subprocess.Popen(chrome_command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=hidden())
            state['processes']['chrome']=process_record(chrome_process,chrome_command,session)
            observer_command=[deps['node'],str(ROOT/'frontend/e2e/manual-observer.mjs'),str(session)]
            with (session/'logs/browser-observer.log').open('ab') as stream:
                observer=subprocess.Popen(observer_command,cwd=ROOT/'frontend',env=env,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,creationflags=hidden())
            state['processes']['observer']=process_record(observer,observer_command,session)
            state['chrome_open_requested'] = True
            write_json(session/'state.private.json', state)
        print('前端：http://127.0.0.1:5173  后端：http://127.0.0.1:8010')
        print('状态：RUNNING / TEST_ONLY / production_accepted=false / accuracy_E=BLOCKED')
        print('本机私有账号说明：' + str(session/'TEST_ACCOUNTS.private.md'))
        print('真实资料副本：' + str(session/'source-copies'))
        print('自动接口/错误日志：' + str(session/'logs'))
        print('请使用 STOP_V426_FRONTEND_MANUAL.cmd 安全停止；不要提交本机私有文件。')
        return 0
    except BaseException:
        for record in state['processes'].values():
            stop_owned(record)
        state['status'] = 'BLOCKED'
        state['blocked_at'] = now()
        write_json(session/'state.private.json',state)
        raise


def check(session=None, verify_originals=True):
    session = load_session(session)
    state = read_json(session/'state.private.json')
    result = {'checked_at':now(), 'session':str(session), 'status':state['status'], 'processes':{}, 'production_accepted':False, 'accuracy_E':'BLOCKED'}
    for name, record in state.get('processes',{}).items():
        result['processes'][name] = {'pid':record['pid'], 'identity_matches':matching_process(record) is not None, 'recorded_children':[{'pid':r['pid'],'identity_matches':matching_process(r) is not None} for r in record.get('children',[])]}
    if state['status']=='RUNNING':
        for name, url in [('backend','http://127.0.0.1:8010/health'),('frontend','http://127.0.0.1:5173')]:
            try:
                result[name+'_health'] = wait_url(url, timeout=3)
            except Blocked:
                result[name+'_health'] = {'status':'FAIL'}
    if verify_originals and (session/'ORIGINAL_BEFORE.private.json').is_file():
        _,source,_ = source_settings()
        current = original_snapshot(source)
        result['original_data_unchanged'] = current == read_json(session/'ORIGINAL_BEFORE.private.json')
        write_json(session/'ORIGINAL_CHECK.private.json',current)
    config=read_json(session/'config.private.json') if (session/'config.private.json').is_file() else {}
    if config and state.get('postgres'):
        _,source,_=source_settings()
        verify_clone_identity(config['TXBUDDY_SAMPLE_DSN'],state['postgres']['database'],source)
        result['postgres_isolation_verified']=True
    running=state['status']=='RUNNING' and all(p['identity_matches'] for p in result['processes'].values()) and {'backend','frontend'}.issubset(result['processes'])
    healthy=all(result.get(n+'_health',{}).get('status')=='ok' for n in PORTS)
    result['passed']=running and healthy and result.get('original_data_unchanged',False) and result.get('postgres_isolation_verified',False)
    write_json(session/'HEALTH_CHECK.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 2


def stop(session=None):
    session=load_session(session)
    state=read_json(session/'state.private.json')
    outcomes={name:stop_owned(record) for name,record in state.get('processes',{}).items()}
    if not (session/'ORIGINAL_BEFORE.private.json').is_file():
        state['status']='BLOCKED'
        write_json(session/'state.private.json',state)
        result={'status':'BLOCKED','reason':'启动尚未建立源快照；无法宣称已核对原始数据','processes':outcomes,'passed':False,'production_accepted':False,'accuracy_E':'BLOCKED'}
        write_json(session/'STOP_RESULT.json',result)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2
    _,source,_=source_settings()
    current=original_snapshot(source)
    unchanged=current==read_json(session/'ORIGINAL_BEFORE.private.json')
    write_json(session/'ORIGINAL_AFTER_STOP.private.json',current)
    result={'stopped_at':now(),'processes':outcomes,'original_data_unchanged':unchanged,'production_accepted':False,'accuracy_E':'BLOCKED','passed':unchanged and all(o['stopped'] for o in outcomes.values()) and all(matching_process(r) is None for r in state.get('processes',{}).values())}
    state['status']='STOPPED' if result['passed'] else 'BLOCKED'
    state['stopped_at']=result['stopped_at']
    write_json(session/'state.private.json',state)
    write_json(session/'STOP_RESULT.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print('私有会话、账号、数据库和日志已保留以供复查；原始数据未删除。')
    return 0 if result['passed'] else 2


SENSITIVE_KEYS = {'password','password_hash','authorization','access_token','token','refresh_token','secret','jwt_secret','internal_token'}


def redact(value):
    if isinstance(value,dict):
        return {k:('[REDACTED]' if k.lower() in SENSITIVE_KEYS or any(s in k.lower() for s in ('password','secret','authorization','access_token','refresh_token','internal_token')) else redact(v)) for k,v in value.items()}
    if isinstance(value,list):
        return [redact(v) for v in value]
    return value


class AuditCapture:
    """Private API evidence without authentication secrets or binary bodies."""
    def __init__(self, app, output):
        self.app,self.output=app,Path(output)

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        request_body=bytearray();response_body=bytearray();status=0;started=time.monotonic();content_type=''
        async def recv():
            event=await receive()
            if event['type']=='http.request' and len(request_body)<65536:
                request_body.extend(event.get('body',b'')[:65536-len(request_body)])
            return event
        async def transmit(event):
            nonlocal status,content_type
            if event['type']=='http.response.start':
                status=event['status']
                content_type=dict(event.get('headers',[])).get(b'content-type',b'').decode(errors='replace')
            if event['type']=='http.response.body' and len(response_body)<65536:
                response_body.extend(event.get('body',b'')[:65536-len(response_body)])
            await send(event)
        failure=None
        try:
            await self.app(scope,recv,transmit)
        except BaseException as exc:
            failure=type(exc).__name__
            raise
        finally:
            def body(raw):
                try:
                    return redact(json.loads(raw))
                except (ValueError,UnicodeDecodeError):
                    return {'captured_bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
            row={'at':now(),'method':scope['method'],'path':scope['path'],'status':status,'duration_ms':round((time.monotonic()-started)*1000,2),'request':body(request_body),'response':body(response_body) if 'json' in content_type else {'content_type':content_type,'captured_bytes':len(response_body)},'exception':failure,'environment':'TEST_ONLY'}
            with self.output.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(row,ensure_ascii=False)+'\n')


def serve_backend(session):
    session=assert_session(session)
    config=read_json(session/'config.private.json')
    for key in ('TXBUDDY_RUNTIME_ROOT','TXBUDDY_STORE_PATH','TXBUDDY_USERS_FILE','TXBUDDY_FILE_ROOT','TXBUDDY_DERIVED_ROOT','TXBUDDY_CORPUS_ROOT','TXBUDDY_PROFESSIONALS_FILE'):
        if not within(config[key],session):
            raise Blocked('运行路径越出本次会话：'+key)
    state=read_json(session/'state.private.json')
    # Reject source credentials or an unexpected role even if a private config is edited.
    _,source,_=source_settings()
    if config.get('TXBUDDY_ENV')!='TEST_ONLY':
        raise Blocked('仅允许 TEST_ONLY 运行环境')
    for key in ('TXBUDDY_SAMPLE_DSN','TXBUDDY_PG_DSN','TX_PG_DSN'):
        if config.get(key)!=config['TXBUDDY_SAMPLE_DSN']:
            raise Blocked('数据库连接未统一到同一隔离克隆：'+key)
    verify_clone_identity(config['TXBUDDY_SAMPLE_DSN'],state['postgres']['database'],source)
    # Build the sanitized environment before replacing process variables;
    # clean_environment needs PATH/SystemRoot to locate Python-side tooling.
    sanitized = clean_environment(config)
    os.environ.clear(); os.environ.update(sanitized)
    for p in (ROOT/'backend',ROOT/'scripts',ROOT.parent/'_CODEX_V425/vendor'):
        sys.path.insert(0,str(p))
    import uvicorn
    from txbuddy.api import app
    uvicorn.run(AuditCapture(app,session/'logs/api-requests.private.jsonl'),host='127.0.0.1',port=8010,access_log=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('start','stop','check','serve-backend'))
    parser.add_argument('--session')
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8',errors='backslashreplace')
    if args.command=='start':
        return start(args.no_browser)
    if args.command=='stop':
        return stop(args.session)
    if args.command=='check':
        return check(args.session)
    serve_backend(args.session)
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Blocked as exc:
        print('BLOCKED: '+str(exc),file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        # Connection errors can contain DSNs; keep full diagnostics private.
        PRIVATE.mkdir(parents=True,exist_ok=True)
        import traceback
        (PRIVATE/'launcher-error.private.log').write_text(traceback.format_exc(),encoding='utf-8')
        print('BLOCKED: '+type(exc).__name__+'；详细错误已保存到 '+str(PRIVATE/'launcher-error.private.log'),file=sys.stderr)
        raise SystemExit(2)
