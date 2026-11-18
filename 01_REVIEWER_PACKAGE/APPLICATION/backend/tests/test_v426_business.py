"""V426: real reference data and isolated negative business regressions."""
from io import BytesIO
from decimal import Decimal
import hashlib
import pytest
from openpyxl import Workbook, load_workbook
from txbuddy.pilot.boq import workbook_boq
from txbuddy.pilot.database import PilotError
from txbuddy.pilot.service import query_prices
from .v24_fixtures import corpus, price_db
from .test_v24_api_auth import secure_client, login, create


def test_real_boq_features_match_original_cells(corpus):
    folder=corpus/'05_REAL_PROJECTS/L1_BUDGET/L1_001/B_REFERENCE'
    count=0
    for suffix in ('土建_工程量清单.xlsx','安装_工程量清单.xlsx'):
        path=next(folder.glob('*'+suffix)); result=workbook_boq(path)
        book=load_workbook(path,data_only=True)
        try:
            for item in result['items']:
                original=book[item['sheet']].cell(item['row'],4).value
                assert item['feature']==str(original or ''), (path.name,item['row'])
                assert item['cells']['feature']==f"D{item['row']}"
            count+=result['count']
        finally: book.close()
    assert count==148


def test_missing_invalid_boq_fields_are_retained_with_raw_cells(tmp_path):
    book=Workbook(); sheet=book.active
    sheet.append(['项目编码','项目名称','项目特征描述','单位','工程量'])
    sheet.append(['010101001001','测试隔离行','测试夹具','m3',2])
    sheet.append([None,'编码缺失测试夹具','原值保留','m3',3])
    sheet.append(['010101001002','数量缺失测试夹具',None,'m3',None])
    sheet.append(['INVALID','编码非法测试夹具',None,'m3',4])
    sheet.append(['010101001003','名称含小计字样的合法测试夹具','不得误删','m3',5])
    path=tmp_path/'TEST_ONLY_INVALID.xlsx';book.save(path);book.close()
    result=workbook_boq(path)
    assert result['count']==5
    assert len(result['items'])==5
    assert result['status']=='IMPORTED_WITH_ERRORS'
    assert len(result['errors'])==3
    assert result['items'][2]['quantity'] is None
    assert result['items'][1]['code']==''
    assert result['items'][1]['raw_cells']
    assert result['items'][4]['feature']=='不得误删'
    assert result['independently_calculated'] is False


def test_api_export_preserves_every_real_boq_field(secure_client,corpus):
    headers=login(secure_client); job=create(secure_client,headers,'V426-BOQ-TEST')
    expected={}
    folder=corpus/'05_REAL_PROJECTS/L1_BUDGET/L1_001/B_REFERENCE'
    for suffix in ('土建_工程量清单.xlsx','安装_工程量清单.xlsx'):
        source=next(folder.glob('*'+suffix));path=job['project_id']+'/'+source.name;raw=source.read_bytes()
        response=secure_client.put('/files/'+path+'?job_id='+job['job_id'],headers={**headers,'X-Content-SHA256':hashlib.sha256(raw).hexdigest()},content=raw)
        assert response.status_code==200,response.text
        response=secure_client.post('/v1/pilot/boq-import',headers=headers,json={'job_id':job['job_id'],'path':path})
        assert response.status_code==200,response.text
        for item in response.json()['items']: expected[(item['file_sha256'],item['sheet'],item['row'])]=item
    response=secure_client.post('/v1/deliverables/'+job['job_id']+'/generate',headers=headers,json={'kind':'sample_report'})
    assert response.status_code==200,response.text
    manifest=response.json();response=secure_client.get(manifest['download_url'],headers=headers)
    assert response.status_code==200
    book=load_workbook(BytesIO(response.content),read_only=True,data_only=True)
    try:
        rows=list(book.active.iter_rows(values_only=True));columns={name:i for i,name in enumerate(rows[1])}
        assert '项目特征' in columns
        assert len(rows)-2==len(expected)==148
        for row in rows[2:]:
            item=expected[(row[columns['SHA256']],row[columns['Sheet']],row[columns['行号']])]
            for col,key in [('编码','code'),('名称','name'),('项目特征','feature'),('单位','unit')]:
                assert (row[columns[col]] or '')==item[key]
            assert Decimal(str(row[columns['原清单工程量']]))==Decimal(item['quantity'])
    finally:book.close()
    assert manifest['accuracy_verification']=='BLOCKED'
    assert manifest['formal_usable'] is False


def test_exact_price_selection_rejects_missing_conditions_and_duplicates(price_db):
    from txbuddy.pilot.service import resolve_price
    with pytest.raises(PilotError,match='MATERIAL_CONDITIONS_REQUIRED'):
        resolve_price(price_db,region='410100-INNER-4RING',period='2026-Q2',material_name='C型钢',spec_model=None,unit='T')
    with price_db.transaction() as c:
        duplicate=c.one("SELECT material_name,spec_model,unit,period FROM material_price WHERE scope_status<>'ERRATA_FOR_Q2' GROUP BY material_name,spec_model,unit,period HAVING count(*)>1 ORDER BY material_name,spec_model LIMIT 1")
    with pytest.raises(PilotError,match='MATERIAL_AMBIGUOUS'):
        resolve_price(price_db,region='410100-INNER-4RING',**duplicate)
    with pytest.raises(PilotError,match='MATERIAL_PRICE_MISSING'):
        resolve_price(price_db,region='410100-INNER-4RING',period='2026-Q2',material_name='真实库不存在的检索负例',spec_model='未提供的规格',unit='未提供的单位')


def test_exact_price_preserves_both_taxes_and_three_errata(price_db):
    from txbuddy.pilot.service import resolve_price
    with price_db.transaction() as c:
        rows=c.rows('SELECT * FROM material_price WHERE record_id IN (?,?,?)',('ZZ-20262-1782','ZZ-20262-1783','ZZ-20262-1784'))
    assert len(rows)==3
    for row in rows:
        with pytest.raises(PilotError,match='PRICE_SOURCE_UNVERIFIED'):
            resolve_price(price_db,region=row['region'],period=row['period'],material_name=row['material_name'],spec_model=row['spec_model'],unit=row['unit'])
        selected=next(x for x in query_prices(price_db,region=row['region'],period=row['period'],q=row['material_name'],spec=row['spec_model'],unit=row['unit'])['items'] if x['record_id']==row['record_id'])
        assert selected['corrected']
        assert len(selected['version_history'])==2
        assert selected['price_incl_tax']!=selected['price_excl_tax']
        assert selected['tax_rate'] is None
        assert selected['source_url']


def test_exact_price_positive_and_correction_source_guard(price_db):
    from txbuddy.pilot.service import resolve_price
    with price_db.transaction() as c:
        row=c.one('SELECT * FROM material_price WHERE record_id=?',('ZZ-20262-1782',))
        sources=c.rows('SELECT DISTINCT s.source_id,s.official_url FROM source_register s JOIN document_inventory d ON d.source_id=s.source_id WHERE d.document_id IN (?,(SELECT source_document_id FROM material_price_correction WHERE record_id=?))',(row['document_id'],row['record_id']))
        for s in sources: c.execute('UPDATE source_register SET official_url=? WHERE source_id=?',('https://example.test/TEST_ONLY',s['source_id']))
    try:
        args=dict(region=row['region'],period=row['period'],material_name=row['material_name'],spec_model=row['spec_model'],unit=row['unit'])
        selected=resolve_price(price_db,**args)
        assert Decimal(selected['price_incl_tax'])==Decimal('492.34')
        assert Decimal(selected['price_excl_tax'])==Decimal('435.70')
        assert len(selected['version_history'])==2
        with price_db.transaction() as c:
            c.execute('UPDATE source_register SET official_url=? WHERE source_id=(SELECT d.source_id FROM document_inventory d JOIN material_price_correction x ON d.document_id=x.source_document_id WHERE x.record_id=?)',('',row['record_id']))
        with pytest.raises(PilotError,match='PRICE_SOURCE_UNVERIFIED'): resolve_price(price_db,**args)
    finally:
        with price_db.transaction() as c:
            for s in sources: c.execute('UPDATE source_register SET official_url=? WHERE source_id=?',(s['official_url'],s['source_id']))
