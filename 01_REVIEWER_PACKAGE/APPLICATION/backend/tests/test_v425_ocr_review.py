"""TEST_ONLY isolated fixtures. These checks never approve the real corpus OCR."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib,json,sqlite3
import pytest
from PIL import Image
from txbuddy.governance.ocr_review import ReviewStore,ReviewError,sha_file
from .test_v24_api_auth import secure_client,login,create

@pytest.fixture
def draft(tmp_path):
    source=tmp_path/'original';source.mkdir();p=source/'TEST_ONLY.pdf';p.write_bytes(b'TEST_ONLY synthetic original')
    images=tmp_path/'derived';images.mkdir();png=images/'page_0001.png';Image.new('RGB',(100,160),'white').save(png)
    payload={'sha256':sha_file(p),'engine':'TEST_ONLY_OCR/1','pages':[{'page':1,'image':str(png),'image_sha256':sha_file(png)}],
      'tokens':[{'page':1,'bbox':[[1,2],[50,2],[50,20],[1,20]],'text':'TEST_ONLY 原始识别1','confidence':0.8}]}
    return source,images,payload

@pytest.fixture
def review_client(secure_client,tmp_path,monkeypatch,draft):
    from txbuddy import api
    store=ReviewStore(tmp_path/'review.sqlite3');monkeypatch.setattr(api.app.state,'review_store',store)
    a=login(secure_client);create(secure_client,a,'V425-ALICE')
    r=store.ingest(draft[2],'V425-ALICE','TEST_ONLY.pdf',draft[0],draft[1]);doc=store.document(r['document_id']);page=store.page(doc['pages'][0]['page_id'])
    return secure_client,store,a,page

def body(page,**updates):
    return {'expected_version':page['version'],'corrected_text':'TEST_ONLY 人工修订2','structured_fields':{'test_only':True},
            'review_status':'IN_REVIEW','reason':'TEST_ONLY fixture',**updates}

def test_original_is_immutable_and_history_persists(review_client,draft):
    c,s,a,p=review_client;before=json.dumps(draft[2],ensure_ascii=False)
    url='/v1/governance/ocr/pages/'+p['page_id']
    r=c.post(url+'/reviews',headers=a,json=body(p));assert r.status_code==200,r.text
    d=r.json();assert d['original_text']==p['original_text'];assert d['corrected_text']!=p['original_text']
    assert d['version']==1 and d['reviewer_id']=='alice' and d['reviewed_at']
    assert d['change_history'][0]['previous_event_sha256']=='' and d['history_chain_valid']
    assert ReviewStore(s.path).page(p['page_id'])['corrected_text']==d['corrected_text']
    assert json.dumps(draft[2],ensure_ascii=False)==before
    assert sha_file(draft[0]/'TEST_ONLY.pdf')==p['source_file_sha256']
    assert not d['formal_usable']

def test_db_rejects_update_delete_original_and_events(review_client):
    c,s,a,p=review_client;c.post('/v1/governance/ocr/pages/'+p['page_id']+'/reviews',headers=a,json=body(p))
    for table in ('ocr_document','ocr_page','ocr_review_event'):
        for stmt in ('DELETE FROM '+table,'UPDATE '+table+' SET '+{'ocr_document':'project_id','ocr_page':'original_text','ocr_review_event':'reason'}[table]+"='tamper'"):
            with pytest.raises(sqlite3.IntegrityError,match='OCR_APPEND_ONLY'):
                with s.connect() as conn:conn.execute(stmt)

def test_stale_revision_cannot_overwrite_corrections(review_client):
    c,s,a,p=review_client;url='/v1/governance/ocr/pages/'+p['page_id']+'/reviews'
    assert c.post(url,headers=a,json=body(p)).status_code==200
    r=c.post(url,headers=a,json=body(p,corrected_text='stale'));assert r.status_code==409
    assert r.json()['detail']['code']=='REVIEW_VERSION_CONFLICT'
    assert s.page(p['page_id'])['version']==1

def test_concurrent_review_atomic_compare_and_append(tmp_path,draft):
    s=ReviewStore(tmp_path/'r.sqlite3');d=s.ingest(draft[2],'P','TEST_ONLY.pdf',draft[0],draft[1]);pid=s.document(d['document_id'])['pages'][0]['page_id']
    def attempt(i):
        try:s.save(pid,expected_version=0,corrected_text=str(i),structured_fields={},review_status='IN_REVIEW',reviewer_id='TEST_ONLY-'+str(i));return 'saved'
        except ReviewError as e:return e.code
    with ThreadPoolExecutor(max_workers=8) as ex:results=list(ex.map(attempt,range(16)))
    assert results.count('saved')==1 and results.count('REVIEW_VERSION_CONFLICT')==15
    assert s.page(pid)['version']==1 and s.verify_chain(pid)

def test_status_human_review_and_return_reason(review_client):
    c,s,a,p=review_client;url='/v1/governance/ocr/pages/'+p['page_id']+'/reviews'
    assert c.post(url,headers=a,json=body(p,review_status='REVIEWED')).status_code==422
    assert c.post(url,headers=a,json=body(p,review_status='RETURNED',reason='')).status_code==422
    # Decisions now bind an explicit submitted snapshot; approval never edits content.
    draft=c.post(url,headers=a,json=body(p));assert draft.status_code==200
    submit_url='/v1/governance/ocr/pages/'+p['page_id']+'/submit'
    submitted=c.post(submit_url,headers=a,json={'expected_version':1,'expected_content_sha256':draft.json()['content_sha256']})
    assert submitted.status_code==200
    snapshot=submitted.json()
    r=c.post(url,headers=a,json=body(snapshot,review_status='REVIEWED',human_confirmed=True,
        submitted_version=snapshot['submitted_version'],expected_content_sha256=snapshot['content_sha256']));assert r.status_code==200,r.text
    assert r.json()['review_status']=='REVIEWED' and not r.json()['formal_usable']
    # A return is a separate decision on a newly submitted version, never a rewrite.
    resubmitted=c.post(submit_url,headers=a,json={'expected_version':r.json()['version'],'expected_content_sha256':r.json()['content_sha256']})
    assert resubmitted.status_code==200,resubmitted.text
    snapshot=resubmitted.json()
    r=c.post(url,headers=a,json=body(snapshot,review_status='RETURNED',
        submitted_version=snapshot['submitted_version'],expected_content_sha256=snapshot['content_sha256']));assert r.status_code==200,r.text
    assert len(r.json()['change_history'])==5 and s.verify_chain(p['page_id'])

def test_professional_signature_cannot_be_claimed_by_client(review_client):
    c,s,a,p=review_client;url='/v1/governance/ocr/pages/'+p['page_id']
    r=c.post(url+'/signoff',headers=a,json=body(p,human_confirmed=True));assert r.status_code==403
    assert r.json()['detail']['code']=='PROFESSIONAL_IDENTITY_REQUIRED'
    assert c.post(url+'/reviews',headers=a,json=body(p,review_status='PROFESSIONALLY_SIGNED',human_confirmed=True)).status_code==403
    assert c.post(url+'/reviews',headers=a,json=body(p,reviewer_id='certified-person')).status_code==422
    assert s.page(p['page_id'])['version']==0

def test_role_without_server_identity_registry_is_rejected(review_client,monkeypatch):
    from txbuddy import api
    from txbuddy.security.tokens import encode_token
    c,s,a,p=review_client;monkeypatch.delenv('TXBUDDY_PROFESSIONALS_FILE',raising=False)
    token=encode_token(subject='alice',settings=api.app.state.security.settings,roles=('verified_professional',))
    r=c.post('/v1/governance/ocr/pages/'+p['page_id']+'/signoff',headers={'Authorization':'Bearer '+token},json=body(p,human_confirmed=True))
    assert r.status_code==403 and s.page(p['page_id'])['version']==0

def test_cross_project_lists_pages_images_and_write_are_isolated(review_client):
    c,s,a,p=review_client;b=login(c,'mallory');base='/v1/governance/ocr'
    assert c.get(base,headers=b).json()['total']==0
    for path in (base+'?project_id=V425-ALICE',base+'/documents/'+p['document_id'],base+'/pages/'+p['page_id'],base+'/pages/'+p['page_id']+'/image',base+'/pages/missing'):
        r=c.get(path,headers=b);assert r.status_code==403,r.text
        assert r.json()['detail']=={'code':'ACL_FORBIDDEN','message':'无权访问该项目'}
    assert c.post(base+'/pages/'+p['page_id']+'/reviews',headers=b,json=body(p)).status_code==403
    assert c.get(base).status_code==401

def test_viewer_reads_but_cannot_edit(review_client):
    from txbuddy import api
    c,s,a,p=review_client;api.app.state.security.acl.grant('V425-ALICE','mallory','VIEWER');b=login(c,'mallory')
    url='/v1/governance/ocr/pages/'+p['page_id']
    assert c.get(url,headers=b).status_code==200
    assert c.post(url+'/reviews',headers=b,json=body(p)).status_code==403

def test_image_hash_checked_at_download(review_client,draft):
    c,s,a,p=review_client;url='/v1/governance/ocr/pages/'+p['page_id']+'/image'
    r=c.get(url,headers=a);assert r.status_code==200 and r.headers['X-Content-SHA256']==p['image_sha256']
    (draft[1]/'page_0001.png').write_bytes(b'broken')
    assert c.get(url,headers=a).status_code==422

def test_import_idempotent_and_no_reviews_created(tmp_path,draft):
    s=ReviewStore(tmp_path/'review.sqlite3')
    first=s.ingest(draft[2],'P','TEST_ONLY.pdf',draft[0],draft[1]);second=s.ingest(draft[2],'P','TEST_ONLY.pdf',draft[0],draft[1])
    assert first['document_id']==second['document_id'] and second['status']=='IDEMPOTENT'
    d=s.documents();assert len(d)==1 and d[0]['review_status']=='NEEDS_EXPERT_REVIEW'
    p=s.page(s.document(first['document_id'])['pages'][0]['page_id'])
    for key in ('source_file_sha256','page_number','image_sha256','OCR_engine','original_text','corrected_text','confidence','reviewer_id','reviewed_at','review_status','change_history'):
        assert key in p
    assert p['version']==0 and p['change_history']==[] and p['reviewer_id'] is None

@pytest.mark.parametrize('kind',['source','image','confidence','path'])
def test_import_rejects_bad_provenance(tmp_path,draft,kind):
    s=ReviewStore(tmp_path/'r.sqlite3');source,images,payload=draft
    path='TEST_ONLY.pdf'
    if kind=='source':payload['sha256']='0'*64
    if kind=='image':payload['pages'][0]['image_sha256']='0'*64
    if kind=='confidence':payload['tokens'][0]['confidence']=float('nan')
    if kind=='path':path='../TEST_ONLY.pdf'
    with pytest.raises((ReviewError,ValueError)):s.ingest(payload,'P',path,source,images)
    assert s.documents()==[]

def test_matrix_and_source_queries_use_database_and_acl(review_client):
    c,s,a,p=review_client
    s.import_catalog('projects',{'projects':[{'project_id':'V425-ALICE','name':'TEST_ONLY','l1_quantity_status':'BLOCKED'}]})
    s.import_catalog('sources',{'sources':[{'source_id':'TEST_ONLY-S','project_id':'V425-ALICE','verification_status':'INCOMPLETE'}]})
    assert c.get('/v1/governance/projects',headers=a).json()['total']==1
    assert c.get('/v1/governance/sources',headers=a).json()['total']==1
    b=login(c,'mallory')
    assert c.get('/v1/governance/projects',headers=b).json()['total']==0
    assert c.get('/v1/governance/sources',headers=b).json()['total']==0
    assert c.get('/v1/governance/sources/TEST_ONLY-S',headers=b).status_code==403
    assert c.get('/v1/governance/projects?project_id=V425-ALICE',headers=b).status_code==403

def test_catalog_snapshot_is_append_only_and_idempotent(tmp_path):
    s=ReviewStore(tmp_path/'r.sqlite3');a=s.import_catalog('projects',{'projects':[]});b=s.import_catalog('projects',{'projects':[]})
    assert a==b
    s.import_catalog('projects',{'projects':[{'project_id':'TEST_ONLY'}]})
    with s.connect() as c:
        assert c.execute('SELECT count(*) FROM governance_catalog_snapshot').fetchone()[0]==2
        assert c.execute('SELECT count(*) FROM governance_catalog_current').fetchone()[0]==1
    assert s.catalog('projects')['projects'][0]['project_id']=='TEST_ONLY'
