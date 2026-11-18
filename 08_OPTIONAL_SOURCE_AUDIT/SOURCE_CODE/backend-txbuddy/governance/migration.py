"""Read-only inventory and explicitly isolated PostgreSQL migration experiments.

This module is never installed as a live backend. Source databases are opened
read-only; only database names containing ``v425_test`` may receive writes.
The table and asset digests deliberately exclude filesystem relocation and
normalize JSON representation, UTC timestamps and SQLite boolean integers.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
import hashlib
import json
import re
import shutil
import sqlite3

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, execute_values


class MigrationSafetyError(ValueError):
    pass


JSON_COLUMNS = {
    'payload', 'basis_pack', 'status_history', 'snapshot', 'response', 'result',
    'history', 'failed_rules', 'tokens_json', 'structured_fields_json', 'payload_json', 'roles', 'discovery_sources',
    'pages_json', 'source_record_json', 'anchor_json', 'details_json',
}
TIME_COLUMNS = {
    'created_at', 'updated_at', 'approved_at', 'added_at', 'judged_at',
    'claimed_at', 'at', 'reviewed_at', 'applied_at',
}
ORDER = [
    'security.user_account', 'security.project_scope', 'security.acl_project', 'security.acl_member', 'security.auth_revoked',
    'storage.file_blob', 'storage.file_version', 'runtime.schema_version', 'runtime.job',
    'runtime.evidence', 'runtime.calc_run', 'runtime.audit_log', 'runtime.idem',
    'runtime.job_document', 'runtime.job_task', 'runtime.job_record', 'runtime.gate_record',
    'corpus.source_register', 'corpus.document_inventory', 'corpus.project_sample',
    'corpus.data_import_job', 'corpus.data_import_audit', 'corpus.project_document',
    'corpus.extracted_field', 'corpus.material_price', 'corpus.material_price_correction',
    'corpus.quota_chapter', 'corpus.quota_item', 'corpus.quota_resource', 'corpus.quota_note',
    'corpus.quota_conversion', 'corpus.fee_rate_matrix', 'corpus.basis_pack', 'corpus.pilot_migration',
    'review.ocr_document', 'review.ocr_page', 'review.ocr_review_event',
    'review.governance_catalog_snapshot', 'review.governance_catalog_current',
    'review.ocr_content','review.ocr_content_version','review.ocr_source_record',
    'review.ocr_asset_anchor','review.ocr_chain_head','review.ocr_event_binding','review.ocr_security_incident',
]


def file_sha(path: Path | str) -> str:
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def require_test_target(dsn: str, *, source_dsn: str | None = None) -> str:
    params = psycopg2.extensions.parse_dsn(dsn)
    name = params.get('dbname', '')
    if not re.fullmatch(r'[a-z][a-z0-9_]*', name) or 'v425_test' not in name:
        raise MigrationSafetyError('DB_ISOLATION_REQUIRED: target database name must contain v425_test')
    if source_dsn:
        source = psycopg2.extensions.parse_dsn(source_dsn)
        keys = ('host', 'hostaddr', 'port', 'dbname')
        if all(params.get(k) == source.get(k) for k in keys):
            raise MigrationSafetyError('SOURCE_TARGET_IDENTICAL')
    return name


def connect_target(dsn: str):
    require_test_target(dsn)
    return psycopg2.connect(dsn, connect_timeout=10, options='-c statement_timeout=120000')


def _canonical(column: str, value: Any, row: dict | None = None):
    if value is None:
        return None
    if column == 'storage_path' and row and row.get('sha256'):
        digest = row['sha256']
        return digest[:2]+'/'+digest
    if column == 'image_path' and row and row.get('image_sha256'):
        return 'ocr/'+row['image_sha256']+'.png'
    if column in JSON_COLUMNS and isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass  # result may legitimately be a decimal string in calc_run.
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if column in TIME_COLUMNS and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        return format(value, 'f')
    return value


def content_digest(columns: list[str], rows: list[tuple]) -> str:
    encoded = []
    for values in rows:
        row = dict(zip(columns, values))
        normalized = [_canonical(name, value, row) for name, value in zip(columns, values)]
        encoded.append(json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                                  separators=(',', ':'), allow_nan=False))
    digest = hashlib.sha256()
    digest.update(json.dumps(columns, ensure_ascii=False).encode('utf-8'))
    digest.update(b'\n')
    for value in sorted(encoded):
        digest.update(value.encode('utf-8'))
        digest.update(b'\n')
    return digest.hexdigest()


@dataclass
class TableData:
    schema: str
    name: str
    columns: list[str]
    rows: list[tuple]
    source: str
    definition: dict

    @property
    def key(self):
        return self.schema+'.'+self.name

    def manifest(self):
        return dict(table=self.key, source=self.source, columns=self.columns,
                    count=len(self.rows), content_sha256=content_digest(self.columns, self.rows),
                    definition=self.definition)


def read_sqlite(path: Path | str, target_schema: str) -> dict[str, TableData]:
    path = Path(path).resolve(strict=True)
    result = {}
    # mode=ro cannot create a missing database or modify a source schema.
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        for name, ddl in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' "
                                      "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            ident = '"'+name.replace('"', '""')+'"'
            columns = conn.execute('PRAGMA table_info('+ident+')').fetchall()
            rows = [tuple(r) for r in conn.execute('SELECT * FROM '+ident).fetchall()]
            table = TableData(target_schema, name, [r[1] for r in columns], rows, str(path),
                dict(ddl=ddl, columns=[dict(name=r[1], type=r[2], nullable=not bool(r[3]),
                     default=r[4], primary_key_order=r[5]) for r in columns],
                     foreign_keys=[list(x) for x in conn.execute('PRAGMA foreign_key_list('+ident+')')],
                     indexes=[list(x) for x in conn.execute('PRAGMA index_list('+ident+')')]))
            result[table.key] = table
        conn.rollback()
    return result


def read_postgres(dsn: str, source_schema='public', target_schema='corpus') -> dict[str, TableData]:
    result = {}
    with closing(psycopg2.connect(dsn, connect_timeout=10)) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s "
                        "AND table_type='BASE TABLE' ORDER BY table_name", (source_schema,))
            names = [r[0] for r in cur.fetchall()]
            for name in names:
                cur.execute(sql.SQL('SELECT * FROM {}.{}').format(sql.Identifier(source_schema), sql.Identifier(name)))
                columns = [x.name for x in cur.description]
                rows = cur.fetchall()
                cur.execute("SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull "
                            "FROM pg_attribute a WHERE a.attrelid=%s::regclass AND a.attnum>0 "
                            "AND NOT a.attisdropped ORDER BY a.attnum", (source_schema+'.'+name,))
                coldefs = [dict(name=r[0], type=r[1], nullable=not r[2]) for r in cur.fetchall()]
                cur.execute('SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=%s::regclass',
                            (source_schema+'.'+name,))
                constraints = [dict(name=r[0], definition=r[1]) for r in cur.fetchall()]
                table = TableData(target_schema, name, columns, rows,
                                  'PostgreSQL/'+source_schema+'.'+name,
                                  dict(columns=coldefs, constraints=constraints))
                result[table.key] = table
        conn.rollback()
    return result


def collect_sources(config: dict) -> dict[str, TableData]:
    runtime = Path(config['TXBUDDY_RUNTIME_ROOT'])
    data = read_postgres(config['TXBUDDY_SAMPLE_DSN'])
    for path, schema in ((runtime/'business.sqlite3', 'runtime'),
                         (runtime/'auth.sqlite3', 'security'),
                         (runtime/'blobs/meta.sqlite3', 'storage'),
                         (runtime/'governance.sqlite3', 'review')):
        if path.exists():
            data.update(read_sqlite(path, schema))
        elif schema != 'review':
            raise MigrationSafetyError('REQUIRED_SOURCE_MISSING: '+str(path))
    usersfile = Path(config['TXBUDDY_USERS_FILE'])
    users = json.loads(usersfile.read_text(encoding='utf-8-sig'))
    userrows = [(name, spec['password_hash'], spec.get('roles', []), spec.get('display_name', name),
                 bool(spec.get('active', True))) for name, spec in sorted(users.items())]
    account = TableData('security', 'user_account', ['username', 'password_hash', 'roles', 'display_name', 'active'],
                        userrows, str(usersfile), {'primary_key':'username'})
    data[account.key] = account
    project_keys = {}
    for key in ('security.acl_project','security.acl_member','runtime.job','storage.file_version','review.ocr_document'):
        if key in data:
            table = data[key]
            for values in table.rows:
                project = dict(zip(table.columns, values)).get('project_id')
                if project:
                    project_keys.setdefault(project, set()).add(key)
    scope = TableData('security','project_scope',['project_id','discovery_sources'],
        [(key,sorted(sources)) for key,sources in sorted(project_keys.items())],
        'DERIVED_PROJECT_KEYS_FROM_RECORDED_REFERENCES', {'primary_key':'project_id',
            'note':'Only existing project keys and discovery sources; no owner or membership is invented.'})
    data[scope.key] = scope
    unknown = set(data)-set(ORDER)
    if unknown:
        raise MigrationSafetyError('UNMAPPED_SOURCE_TABLES: '+', '.join(sorted(unknown)))
    return data


def snapshot_manifest(data: dict[str, TableData]) -> dict:
    tables = [data[k].manifest() for k in ORDER if k in data]
    summary = [{k:t[k] for k in ('table','count','content_sha256')} for t in tables]
    digest = hashlib.sha256(json.dumps(summary, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return dict(tables=tables, batch_sha256=digest, table_count=len(tables),
                row_count=sum(t['count'] for t in tables))


def initialize_target(dsn: str, sql_dir: Path | str):
    require_test_target(dsn)
    sql_dir = Path(sql_dir)
    texts = [ (sql_dir/name).read_text(encoding='utf-8-sig') for name in
        ('003_repository_runtime_postgresql.sql', '004_sample_pilot.sql', '005_v425_unification_poc.sql')]
    checksum = hashlib.sha256('\n'.join(texts).encode()).hexdigest()
    optional=sql_dir/'006_v426_ocr_audit_poc.sql'
    extension=optional.read_text(encoding='utf-8-sig') if optional.exists() else None
    with closing(connect_target(dsn)) as conn, conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('txbuddy:v425:poc:ddl'))")
            cur.execute("SELECT to_regclass('migration_control.version')")
            existing=bool(cur.fetchone()[0])
            if existing:
                cur.execute("SELECT checksum FROM migration_control.version WHERE version='V425_POC_005'")
                found=cur.fetchone()
                if not found or found[0]!=checksum:
                    raise MigrationSafetyError('MIGRATION_CHECKSUM_DRIFT')
            else:
                cur.execute('CREATE SCHEMA runtime; SET LOCAL search_path=runtime,pg_catalog')
                cur.execute(texts[0])
                cur.execute('CREATE SCHEMA corpus; SET LOCAL search_path=corpus,pg_catalog')
                cur.execute(texts[1])
                cur.execute('CREATE TABLE corpus.pilot_migration(version TEXT PRIMARY KEY,sha256 TEXT NOT NULL)')
                cur.execute('SET LOCAL search_path=pg_catalog')
                cur.execute(texts[2])
                cur.execute('INSERT INTO migration_control.version(version,checksum) VALUES(%s,%s)', ('V425_POC_005', checksum))
            applied=False
            if extension:
                ext_sha=hashlib.sha256(extension.encode()).hexdigest()
                cur.execute("SELECT checksum FROM migration_control.version WHERE version='V426_POC_006'")
                found=cur.fetchone()
                if found and found[0]!=ext_sha:
                    raise MigrationSafetyError('MIGRATION_CHECKSUM_DRIFT')
                if not found:
                    cur.execute(extension)
                    cur.execute('INSERT INTO migration_control.version(version,checksum) VALUES(%s,%s)',('V426_POC_006',ext_sha))
                    applied=True
    return not existing or applied


def _adapt(table: TableData, values: tuple):
    row = dict(zip(table.columns, values))
    result = []
    for name, value in zip(table.columns, values):
        if name == 'storage_path':
            value = _canonical(name, value, row)
        elif name == 'image_path':
            value = _canonical(name, value, row)
        if (table.schema in ('runtime', 'review') and name in JSON_COLUMNS and
                not (table.name == 'calc_run' and name == 'result')) or (table.schema == 'security' and name in ('roles','discovery_sources')):
            if value is not None:
                value = Json(json.loads(value) if isinstance(value, str) else value)
        if (table.key == 'runtime.job_document' and name == 'verified') or (table.key == 'security.user_account' and name == 'active'):
            value = bool(value)
        result.append(value)
    return tuple(result)


def target_manifest(dsn: str, data: dict[str, TableData]) -> dict:
    require_test_target(dsn)
    records = []
    with closing(connect_target(dsn)) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor() as cur:
            for key in ORDER:
                if key not in data:
                    continue
                table = data[key]
                cur.execute(sql.SQL('SELECT {} FROM {}.{}').format(
                    sql.SQL(',').join(map(sql.Identifier, table.columns)), sql.Identifier(table.schema), sql.Identifier(table.name)))
                rows = cur.fetchall()
                records.append(dict(table=key, count=len(rows), content_sha256=content_digest(table.columns, rows)))
        conn.rollback()
    return {'tables':records}


def verify_target(dsn: str, data: dict[str, TableData]):
    expected = snapshot_manifest(data)
    actual = target_manifest(dsn, data)
    wanted = {x['table']:x for x in expected['tables']}
    result = []
    for got in actual['tables']:
        source = wanted[got['table']]
        result.append(dict(table=got['table'], source_count=source['count'], target_count=got['count'],
            source_content_sha256=source['content_sha256'], target_content_sha256=got['content_sha256'],
            count_equal=source['count']==got['count'], hash_equal=source['content_sha256']==got['content_sha256']))
    return dict(passed=all(r['count_equal'] and r['hash_equal'] for r in result), tables=result)


def load_target(dsn: str, data: dict[str, TableData], *, fail_after_table: str | None = None):
    require_test_target(dsn)
    manifest = snapshot_manifest(data)
    with closing(connect_target(dsn)) as conn, conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('txbuddy:v425:poc:data'))")
            cur.execute('SET CONSTRAINTS ALL DEFERRED')
            for key in ORDER:
                if key not in data:
                    continue
                table = data[key]
                if table.rows:
                    statement = sql.SQL('INSERT INTO {}.{} ({}) VALUES %s ON CONFLICT DO NOTHING').format(
                        sql.Identifier(table.schema), sql.Identifier(table.name),
                        sql.SQL(',').join(map(sql.Identifier, table.columns))).as_string(conn)
                    for start in range(0, len(table.rows), 1000):
                        execute_values(cur, statement, [_adapt(table, row) for row in table.rows[start:start+1000]], page_size=1000)
                if fail_after_table == key:
                    raise RuntimeError('TEST_ONLY_MIGRATION_FAILURE_AFTER '+key)
            # Do not silently accept an altered duplicate key. Validate before commit.
            for key in ORDER:
                if key not in data:
                    continue
                table = data[key]
                cur.execute(sql.SQL('SELECT {} FROM {}.{}').format(
                    sql.SQL(',').join(map(sql.Identifier, table.columns)), sql.Identifier(table.schema), sql.Identifier(table.name)))
                got = cur.fetchall()
                if len(got) != len(table.rows) or content_digest(table.columns, got) != content_digest(table.columns, table.rows):
                    raise MigrationSafetyError('TARGET_DATA_CONFLICT: '+key)
            cur.execute('INSERT INTO migration_control.batch(batch_sha256,table_count,row_count) VALUES(%s,%s,%s) '
                        'ON CONFLICT DO NOTHING', (manifest['batch_sha256'], manifest['table_count'], manifest['row_count']))
            for key in ('runtime.audit_log','runtime.job_document','runtime.job_record','runtime.gate_record'):
                cur.execute('SELECT pg_get_serial_sequence(%s,%s)', (key, 'id'))
                sequence = cur.fetchone()[0]
                if sequence:
                    cur.execute(sql.SQL('SELECT setval(%s, COALESCE(MAX(id),0)+1,false) FROM {}.{}').format(
                        *map(sql.Identifier, key.split('.'))), (sequence,))
    return manifest


def copy_assets(data: dict[str, TableData], destination: Path | str) -> list[dict]:
    """Copy content-addressed blobs and OCR page images, verifying each byte hash."""
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    copied = []
    for key, path_col, sha_col in (('storage.file_blob','storage_path','sha256'),
                                    ('review.ocr_page','image_path','image_sha256')):
        if key not in data:
            continue
        table = data[key]
        for values in table.rows:
            row = dict(zip(table.columns, values))
            source = Path(row[path_col]).resolve(strict=True)
            digest = row[sha_col]
            if not re.fullmatch('[0-9a-f]{64}', digest) or file_sha(source) != digest:
                raise MigrationSafetyError('ASSET_HASH_MISMATCH')
            relative = _canonical(path_col, str(source), row)
            target = destination/relative
            if not target.resolve().is_relative_to(destination) or target.resolve() == source:
                raise MigrationSafetyError('ASSET_ISOLATION_REQUIRED')
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if file_sha(target) != digest:
                    raise MigrationSafetyError('EXISTING_ASSET_CONFLICT')
            else:
                shutil.copyfile(source, target)
            if file_sha(target) != digest:
                raise MigrationSafetyError('COPIED_ASSET_HASH_MISMATCH')
            copied.append(dict(key=relative, sha256=digest, bytes=target.stat().st_size,
                               kind='ocr_page' if key.startswith('review') else 'file_blob'))
    return copied


def grant_test_role(dsn: str, role: str):
    """Role must have been created explicitly by the isolated experiment runner."""
    require_test_target(dsn)
    if not re.fullmatch(r'txbuddy_v425_test_[a-z0-9_]+', role):
        raise MigrationSafetyError('ROLE_ISOLATION_REQUIRED')
    with closing(connect_target(dsn)) as conn, conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL('GRANT USAGE ON SCHEMA runtime,security,storage,review TO {}').format(sql.Identifier(role)))
            cur.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON runtime.job,runtime.job_document,runtime.job_task,'
                'runtime.job_record,runtime.gate_record,runtime.calc_run,runtime.evidence,'
                'storage.file_version,review.ocr_document,review.ocr_page,review.ocr_review_event TO {}').format(sql.Identifier(role)))
            cur.execute(sql.SQL('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA runtime TO {}').format(sql.Identifier(role)))

