from .test_v24_api_auth import secure_client,login,create


def test_alice_mallory_cross_project_read_write_and_list(secure_client):
    a=login(secure_client); b=login(secure_client,'mallory'); job=create(secure_client,a)
    jid=job['job_id']
    for path in (f'/v1/job/{jid}',f'/v1/job/{jid}/records?kind=sample_boq','/v1/job/no-such-id'):
        r=secure_client.get(path,headers=b)
        assert r.status_code==403,r.text
        assert r.json()['detail']=={'code':'ACL_FORBIDDEN','message':'无权访问该项目'}
    assert secure_client.post(f'/v1/job/{jid}/records',json={'kind':'boq','payload':{}},headers=b).status_code==403
    assert secure_client.get('/v1/jobs',headers=b).json()['total']==0
    assert secure_client.get('/v1/jobs',headers=a).json()['total']==1
    assert secure_client.post('/v1/job',headers=b,json={**job,'basis_pack':job['basis_pack']}).status_code==403


def test_acl_persists_and_x_actor_cannot_impersonate(secure_client):
    from txbuddy import api
    from txbuddy.security.runtime import PersistentAcl
    a=login(secure_client); job=create(secure_client,a)
    assert PersistentAcl(api.app.state.security.acl.path).role_of(job['project_id'],'alice').value=='OWNER'
    b={**login(secure_client,'mallory'),'X-Actor':'alice'}
    assert secure_client.get('/v1/job/'+job['job_id'],headers=b).status_code==403


def test_viewer_cannot_write(secure_client):
    a=login(secure_client); job=create(secure_client,a); b=login(secure_client,'mallory')
    from txbuddy import api
    api.app.state.security.acl.grant(job['project_id'],'mallory','VIEWER')
    assert secure_client.get('/v1/job/'+job['job_id'],headers=b).status_code==200
    assert secure_client.post('/v1/job/'+job['job_id']+'/tasks',headers=b,json={'node':'IMPORT'}).status_code==403

def test_calc_and_evidence_resolve_project_acl(secure_client):
    a=login(secure_client);job=create(secure_client,a);b=login(secure_client,'mallory')
    calc=secure_client.post('/v1/quantity/rule-calc',headers=a,json={'job_id':job['job_id'],'basis_pack':job['basis_pack'],'rule_code':'QTY.BOQ.CONC.COLUMN.001','params':{'b':'0.5','h':'0.5','H':'3'}})
    assert calc.status_code==200,calc.text
    for path in ('/v1/calc/'+calc.json()['calc_run_id'],'/v1/evidence/'+calc.json()['evidence_id']):
        assert secure_client.get(path,headers=a).status_code==200
        assert secure_client.get(path,headers=b).status_code==403
