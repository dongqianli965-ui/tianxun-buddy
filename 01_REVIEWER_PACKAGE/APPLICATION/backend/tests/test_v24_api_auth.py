import time
import pytest
from fastapi.testclient import TestClient
from txbuddy import api
from txbuddy.security.users import UserDirectory
from txbuddy.security.tokens import encode_token
from txbuddy.security.runtime import PersistentAcl, PersistentRevocations


@pytest.fixture
def secure_client(tmp_path,monkeypatch):
    from txbuddy.store import EvidenceStore
    monkeypatch.setattr(api,'store',EvidenceStore(str(tmp_path/'business.sqlite3')))
    ctx=api.app.state.security
    monkeypatch.setattr(ctx,'directory',UserDirectory.from_plaintext({
        'alice':{'password':'alice-test-password','roles':('estimator',)},
        'mallory':{'password':'mallory-test-password','roles':('estimator',)}}))
    acl=PersistentAcl(tmp_path/'auth.sqlite3')
    monkeypatch.setattr(ctx,'acl',acl); monkeypatch.setattr(ctx,'revocations',PersistentRevocations(acl))
    with TestClient(api.app) as c: yield c


def login(c,name='alice'):
    r=c.post('/auth/login',json={'username':name,'password':name+'-test-password'})
    assert r.status_code==200,r.text
    return {'Authorization':'Bearer '+r.json()['access_token']}


def create(c,headers,project='V24-ALICE'):
    r=c.post('/v1/job',json={'project_id':project,'name':'真实资料样本试点','business_line':'L1',
        'basis_pack':{'basis_pack_id':'TEST_ONLY-SAMPLE','test_only':True,
            'region_code':'410100','region_level':'CITY','major':'BUILDING','effective_date':'2026-01-01',
            'measurement_standard_version':'GB/T 50854-2024','pricing_standard_version':'GB/T 50500-2024',
            'rule_version_scope':'HENAN_2025','precision_rule_version':'P-V1','status':'DRAFT','sources':[]}},headers=headers)
    assert r.status_code==200,r.text
    return r.json()


def test_business_requires_credentials_even_with_x_actor(secure_client):
    for path in ('/v1/rules','/v1/jobs','/v1/job/nonexistent','/auth/me'):
        r=secure_client.get(path,headers={'X-Actor':'alice'})
        assert r.status_code==401,r.text
        assert r.headers['www-authenticate'].startswith('Bearer')


def test_login_logout_and_expiry_are_real(secure_client):
    h=login(secure_client)
    assert secure_client.get('/auth/me',headers=h).json()['subject']=='alice'
    assert secure_client.post('/auth/logout',headers=h).status_code==200
    assert secure_client.get('/v1/rules',headers=h).status_code==401
    t=encode_token(subject='alice',settings=api.app.state.security.settings,now=int(time.time())-100,ttl_seconds=1)
    assert secure_client.get('/v1/rules',headers={'Authorization':'Bearer '+t}).status_code==401


def test_forged_token_is_rejected(secure_client):
    h=login(secure_client); h['Authorization']+='forged'
    assert secure_client.get('/v1/rules',headers=h).status_code==401

def test_client_cannot_self_approve_license_for_formal_numbers(secure_client):
    h=login(secure_client);job=create(secure_client,h)
    basis={**job['basis_pack'],'basis_pack_id':'UNREGISTERED-CLAIMED-LICENSE','status':'LOCKED','test_only':False,
        'sources':[{'source_id':'S','title':'自述依据','source_type':'QUOTA','license_status':'LICENSED','verified':True}]}
    r=secure_client.post('/v1/quantity/calc',headers=h,json={'basis_pack':basis,'formal_calculation':True,'operation':'RECTANGULAR_AREA',
        'params':{'length':'2','width':'3'},'unit':'m2','rule_code':'R','rule_version':'1','standard_ref':'S'})
    assert r.status_code==422,r.text
    assert 'TRUSTED_BASIS_NOT_REGISTERED' in r.json()['detail']['admission_reasons']

def test_fixed_quantity_evidence_is_bound_to_job_and_acl(secure_client):
    owner=login(secure_client,'alice')
    job=create(secure_client,owner,'V24-QUANTITY-OWNER')
    request={
        'job_id':job['job_id'],
        'basis_pack':job['basis_pack'],
        'operation':'RECTANGULAR_AREA',
        'params':{'length':'2','width':'3'},
        'unit':'m2','rule_code':'RECTANGLE','rule_version':'1','standard_ref':'TEST_ONLY',
    }
    calculated=secure_client.post('/v1/quantity/calc',headers=owner,json=request)
    assert calculated.status_code==200,calculated.text
    evidence_id=calculated.json()['evidence_id']

    # The creating project member must be able to read its calculation evidence.
    evidence=secure_client.get(f'/v1/evidence/{evidence_id}',headers=owner)
    assert evidence.status_code==200,evidence.text
    assert evidence.json()['job_id']==job['job_id']

    # Evidence remains isolated from another user's project.
    outsider=login(secure_client,'mallory')
    denied=secure_client.get(f'/v1/evidence/{evidence_id}',headers=outsider)
    assert denied.status_code==403,denied.text

def test_approve_node_rejects_body_claimed_actor_and_professional(secure_client):
    owner=login(secure_client,'alice')
    job=create(secure_client,owner,'V24-APPROVAL-OWNER')
    task=secure_client.post(f"/v1/job/{job['job_id']}/tasks",headers=owner,
        json={'node':'APPROVE','payload':{}})
    assert task.status_code==200,task.text
    task_id=task.json()['task_id']
    assert secure_client.post(f'/v1/task/{task_id}/start',headers=owner,
        json={'actor':'forged-editor','role':'EDITOR'}).json()['status']=='RUNNING'
    assert secure_client.post(f'/v1/task/{task_id}/complete',headers=owner,
        json={'actor':'forged-editor','role':'EDITOR','result':{}}).json()['status']=='WAITING_APPROVAL'

    forged=secure_client.post(f'/v1/task/{task_id}/approve',headers=owner,
        json={'actor':'licensed-professional','role':'PRINCIPAL',
              'license':'REGISTERED_COST_ENGINEER'})
    assert forged.status_code==403,forged.text
    assert forged.json()['detail']['code']=='TASK_ACTOR_MISMATCH'

    self_claim=secure_client.post(f'/v1/task/{task_id}/approve',headers=owner,
        json={'actor':'alice','role':'PRINCIPAL','license':'REGISTERED_COST_ENGINEER'})
    assert self_claim.status_code==403,self_claim.text
    assert self_claim.json()['detail']['code']=='PROFESSIONAL_IDENTITY_REQUIRED'
    assert secure_client.get(f'/v1/task/{task_id}',headers=owner).json()['status']=='WAITING_APPROVAL'
