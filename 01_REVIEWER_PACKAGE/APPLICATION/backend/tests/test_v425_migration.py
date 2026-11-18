"""Isolated migration safety, byte preservation and real PostgreSQL behavior.

Every fixture is TEST_ONLY in a temporary source or newly-created v425_test
database. Missing PostgreSQL is a failure, never a skipped acceptance check.
"""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import uuid

import psycopg2
from psycopg2 import sql
import pytest
from PIL import Image
from txbuddy.governance.migration import (
    MigrationSafetyError, TableData, collect_sources, content_digest, copy_assets,
    file_sha, grant_test_role, initialize_target, load_target, read_sqlite,
    require_test_target, snapshot_manifest, target_manifest, verify_target,
)
from txbuddy.governance.ocr_review import ReviewStore
from txbuddy.security.runtime import PersistentAcl
from txbuddy.security.users import hash_password
from txbuddy.storage.filestore import FileStore
from txbuddy.store import EvidenceStore

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('v425_migration_acceptance',ROOT/'scripts/v425_migration_poc.py')
ACCEPTANCE=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACCEPTANCE)


@pytest.mark.parametrize('database', ['txbuddy','txbuddy_sample_pilot','production','test_only','v425_live'])
def test_refuses_nonisolated_database_names(database):
    with pytest.raises(MigrationSafetyError, match='DB_ISOLATION_REQUIRED'):
        require_test_target('host=127.0.0.1 dbname='+database)


def test_refuses_same_source_target_even_with_test_name():
    dsn='host=127.0.0.1 port=55432 dbname=txbuddy_v425_test_same'
    with pytest.raises(MigrationSafetyError,match='SOURCE_TARGET_IDENTICAL'):
        require_test_target(dsn,source_dsn=dsn)


def test_content_hash_ignores_only_declared_representations():
    cols=['id','payload','created_at','verified']
    sqlite_rows=[('x','{"中文":1,"nested":[true,null]}','2026-11-16T08:00:00+08:00',0)]
    pg_rows=[('x',{'nested':[True,None],'中文':1},datetime(2026,11,16,tzinfo=timezone.utc),False)]
    assert content_digest(cols,sqlite_rows)==content_digest(cols,pg_rows)
    changed=[('x','{"中文":2,"nested":[true,null]}','2026-11-16T08:00:00+08:00',0)]
    assert content_digest(cols,changed)!=content_digest(cols,pg_rows)
    assert content_digest(['id','original_text'],[('x','1.00')])!=content_digest(['id','original_text'],[('x','1.0')])


def test_content_hash_preserves_duplicate_rows_and_is_order_independent():
    rows=[('a',1),('b',2)]
    assert content_digest(['id','n'],rows)==content_digest(['id','n'],list(reversed(rows)))
    assert content_digest(['id','n'],rows)!=content_digest(['id','n'],rows+[rows[0]])


def test_readonly_inventory_never_creates_missing_source(tmp_path):
    missing=tmp_path/'missing.sqlite3'
    with pytest.raises(FileNotFoundError):
        read_sqlite(missing,'runtime')
    assert not missing.exists()


@pytest.fixture
def source_data(tmp_path,monkeypatch):
    runtime=tmp_path/'TEST_ONLY_runtime';runtime.mkdir()
    store=EvidenceStore(str(runtime/'business.sqlite3'))
    pack={'basis_pack_id':'TEST_ONLY-V425','test_only':True,'region_code':'410100','region_level':'CITY',
        'major':'BUILDING','effective_date':'2026-01-01','measurement_standard_version':'TEST_ONLY',
        'pricing_standard_version':'TEST_ONLY','rule_version_scope':'HENAN_2025',
        'precision_rule_version':'P-V1','status':'DRAFT','sources':[]}
    store.create_job('TEST_ONLY-job','TEST_ONLY-project','TEST_ONLY 项目','L1',pack,'alice')
    acl=PersistentAcl(runtime/'auth.sqlite3')
    # The real grant() behavior does not populate acl_project. Migration must
    # preserve that owner-claim registry and derive project existence separately.
    acl.grant('TEST_ONLY-project','alice','OWNER',granted_by='alice')
    filestore=FileStore(runtime/'blobs')
    ref=filestore.put(b'TEST_ONLY original unchanged bytes','TEST_ONLY-project/source.txt',project_id='TEST_ONLY-project',actor='alice')
    store.add_document('TEST_ONLY-job',ref.doc_key,str(ref.version),ref.sha256,False)
    store.put_record('TEST_ONLY-job','TEST_ONLY_record',{'nested':[1,True,None],'中文':'样本'})
    corpus=tmp_path/'TEST_ONLY_corpus';corpus.mkdir();source=corpus/'source.pdf';source.write_bytes(b'TEST_ONLY source')
    derived=tmp_path/'TEST_ONLY_derived';derived.mkdir();png=derived/'page.png';Image.new('RGB',(100,120),'white').save(png)
    reviews=ReviewStore(runtime/'governance.sqlite3')
    doc=reviews.ingest({'sha256':file_sha(source),'engine':'TEST_ONLY_ENGINE',
        'pages':[{'page':1,'image':str(png),'image_sha256':file_sha(png)}],
        'tokens':[{'page':1,'text':'TEST_ONLY 原文100','confidence':0.75,
                   'bbox':[[1,1],[60,1],[60,20],[1,20]]}]},
        'TEST_ONLY-project','source.pdf',corpus,derived)
    page=reviews.document(doc['document_id'])['pages'][0]['page_id']
    reviews.save(page,expected_version=0,corrected_text='TEST_ONLY 修订101',structured_fields={'test_only':True},
        review_status='IN_REVIEW',reviewer_id='alice',reason='TEST_ONLY fixture')
    users=tmp_path/'TEST_ONLY_users.json'
    users.write_text(json.dumps({'alice':{'password_hash':hash_password('TEST_ONLY-password'),
        'roles':['estimator'],'display_name':'TEST_ONLY Alice'}}),encoding='utf-8')
    monkeypatch.setattr('txbuddy.governance.migration.read_postgres',lambda *args,**kwargs:{})
    config={'TXBUDDY_RUNTIME_ROOT':str(runtime),'TXBUDDY_USERS_FILE':str(users),
            'TXBUDDY_SAMPLE_DSN':'host=127.0.0.1 dbname=TEST_ONLY_read_mock'}
    return collect_sources(config),config


def test_sqlite_inventory_and_asset_copy_preserve_original_bytes(source_data,tmp_path):
    data,config=source_data
    paths=[Path(config['TXBUDDY_RUNTIME_ROOT'])/n for n in ('business.sqlite3','auth.sqlite3','governance.sqlite3','blobs/meta.sqlite3')]
    before=[file_sha(p) for p in paths]
    assets=copy_assets(data,tmp_path/'copied')
    assert len(assets)==2
    assert all(file_sha(tmp_path/'copied'/r['key'])==r['sha256'] for r in assets)
    assert [file_sha(p) for p in paths]==before
    assert data['security.acl_project'].rows==[]
    assert data['security.project_scope'].rows[0][0]=='TEST_ONLY-project'


def test_detects_corrupt_original_or_existing_copy(source_data,tmp_path):
    data,_=source_data
    blob=data['storage.file_blob'];row=dict(zip(blob.columns,blob.rows[0]))
    dest=tmp_path/'copy';key=row['sha256'][:2]+'/'+row['sha256'];(dest/key).parent.mkdir(parents=True)
    (dest/key).write_bytes(b'TEST_ONLY corrupt destination')
    with pytest.raises(MigrationSafetyError,match='EXISTING_ASSET_CONFLICT'):
        copy_assets(data,dest)
    Path(row['storage_path']).write_bytes(b'TEST_ONLY tampered source')
    with pytest.raises(MigrationSafetyError,match='ASSET_HASH_MISMATCH'):
        copy_assets(data,tmp_path/'another-copy')


def test_refuses_copying_assets_onto_their_source(source_data):
    data,config=source_data
    with pytest.raises(MigrationSafetyError,match='ASSET_ISOLATION_REQUIRED'):
        copy_assets(data,Path(config['TXBUDDY_RUNTIME_ROOT'])/'blobs')


def test_new_unmapped_source_table_stops_migration(source_data):
    _,config=source_data
    path=Path(config['TXBUDDY_RUNTIME_ROOT'])/'governance.sqlite3'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE TEST_ONLY_future_table(id TEXT PRIMARY KEY)')
    with pytest.raises(MigrationSafetyError,match='UNMAPPED_SOURCE_TABLES'):
        collect_sources(config)


@pytest.fixture
def pg_target(source_data):
    admin=os.environ.get('TXBUDDY_PG_DSN') or os.environ.get('TX_PG_DSN')
    if not admin:
        pytest.fail('V425 migration requires an isolated PostgreSQL DSN; skipping is prohibited')
    require_test_target(admin)
    dsn=ACCEPTANCE.new_database(admin,'txbuddy_v425_test_pytest_'+uuid.uuid4().hex[:20])
    assert initialize_target(dsn,ROOT/'sql') is True
    data,_=source_data
    return dsn,data


def test_postgresql_real_roundtrip_and_repeat_import_preserve_every_column(pg_target):
    dsn,data=pg_target
    load_target(dsn,data)
    result=verify_target(dsn,data)
    assert result['passed'],result
    assert all(r['count_equal'] and r['hash_equal'] for r in result['tables'])
    before=target_manifest(dsn,data)
    assert initialize_target(dsn,ROOT/'sql') is False
    load_target(dsn,data)
    assert target_manifest(dsn,data)==before


def test_postgresql_failure_rolls_back_all_tables_and_retry_succeeds(pg_target):
    dsn,data=pg_target
    with pytest.raises(RuntimeError,match='TEST_ONLY_MIGRATION_FAILURE_AFTER'):
        load_target(dsn,data,fail_after_table='runtime.job_document')
    assert all(r['count']==0 for r in target_manifest(dsn,data)['tables'])
    load_target(dsn,data)
    assert verify_target(dsn,data)['passed']


def test_postgresql_conflicting_duplicate_is_rejected_without_partial_import(pg_target):
    dsn,data=pg_target
    load_target(dsn,data);before=target_manifest(dsn,data)
    changed=copy.deepcopy(data);table=changed['runtime.job'];idx=table.columns.index('name')
    row=list(table.rows[0]);row[idx]='TEST_ONLY conflicting project name';table.rows[0]=tuple(row)
    with pytest.raises(MigrationSafetyError,match='TARGET_DATA_CONFLICT'):
        load_target(dsn,changed)
    assert target_manifest(dsn,data)==before


def test_postgresql_real_rls_and_concurrent_claims(pg_target):
    dsn,data=pg_target;load_target(dsn,data)
    role='txbuddy_v425_test_role_'+uuid.uuid4().hex[:16]
    ACCEPTANCE.execute(dsn,sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
    try:
        grant_test_role(dsn,role)
        isolation=ACCEPTANCE.permission_checks(dsn,role,data)
        assert isolation['passed'],isolation
        assert isolation['raw_catalog_snapshot_denied'] and isolation['raw_catalog_current_denied']
        concurrency=ACCEPTANCE.concurrency_check(dsn,workers=16)
        assert concurrency['passed'] and concurrency['winners']==1,concurrency
    finally:
        ACCEPTANCE.execute(dsn,sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        ACCEPTANCE.execute(dsn,sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
    assert verify_target(dsn,data)['passed']


def test_postgresql_ocr_original_and_history_cannot_be_rewritten(pg_target):
    dsn,data=pg_target;load_target(dsn,data)
    chains=ACCEPTANCE.ocr_chain_check(dsn)
    assert chains['passed'] and chains['events_checked']==1 and chains['page_chains_checked']==1,chains
    for table in ('ocr_document','ocr_page','ocr_review_event'):
        for operation in ('UPDATE','DELETE'):
            statement=('UPDATE review.'+table+' SET '+('original_text=original_text' if table=='ocr_page' else
                'document_id=document_id' if table=='ocr_document' else 'corrected_text=corrected_text')) if operation=='UPDATE' else 'DELETE FROM review.'+table
            with pytest.raises(psycopg2.Error,match='OCR_APPEND_ONLY'):
                ACCEPTANCE.execute(dsn,statement)
    assert verify_target(dsn,data)['passed']


def test_postgresql_migration_checksum_drift_refused(pg_target,tmp_path):
    dsn,_=pg_target;directory=tmp_path/'sql';directory.mkdir()
    for name in ('003_repository_runtime_postgresql.sql','004_sample_pilot.sql','005_v425_unification_poc.sql'):
        shutil.copyfile(ROOT/'sql'/name,directory/name)
    path=directory/'005_v425_unification_poc.sql';path.write_text(path.read_text(encoding='utf-8')+'\n-- TEST_ONLY drift\n',encoding='utf-8')
    with pytest.raises(MigrationSafetyError,match='MIGRATION_CHECKSUM_DRIFT'):
        initialize_target(dsn,directory)
