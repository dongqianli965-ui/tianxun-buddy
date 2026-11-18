from .v24_fixtures import corpus,price_db
from txbuddy.pilot.importer import import_corpus
from txbuddy.pilot import PilotDatabase,PilotError
from txbuddy.pilot.service import source_detail
import pytest

def test_source_reverse_lookup_and_idempotence(price_db,corpus):
    before=price_db.counts();r=import_corpus(price_db,corpus);assert r['status']=='IDEMPOTENT';assert price_db.counts()==before
    with price_db.transaction() as c: row=c.one('SELECT * FROM material_price LIMIT 1')
    d=source_detail(price_db,row['document_id'])
    assert d['document']['sha256']==row['sha256'] and d['source'] is not None
    assert len(d['price_records'])>=939
    assert row['source_page']>0 and row['source_row']>0 and row['license_status']

def test_failed_import_rolls_back_every_business_row(tmp_path,corpus):
    d=PilotDatabase(tmp_path/'rollback-sample.sqlite3');d.migrate()
    with pytest.raises(PilotError,match='IMPORT_INJECTED_FAILURE'):import_corpus(d,corpus,fail_after=750)
    counts=d.counts()
    assert all(n==0 for name,n in counts.items() if name not in ('data_import_job','data_import_audit'))
    assert counts['data_import_job']==counts['data_import_audit']==1
    with d.transaction() as c:
        assert c.one('SELECT status FROM data_import_job')['status']=='FAILED'
        assert c.one('SELECT action FROM data_import_audit')['action']=='IMPORT_ROLLBACK'

def test_dev_db_cannot_be_import_target(tmp_path):
    with pytest.raises(PilotError,match='DB_ISOLATION_REQUIRED'):PilotDatabase(tmp_path/'txbuddy-dev.sqlite3')
    with pytest.raises(PilotError,match='DB_ISOLATION_REQUIRED'):PilotDatabase('postgresql://localhost/txbuddy')
