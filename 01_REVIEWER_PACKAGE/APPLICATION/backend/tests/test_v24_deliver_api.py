from .test_v24_api_auth import secure_client,login,create
def test_formal_generation_cannot_bypass_g4_and_professional_signoff(secure_client):
    a=login(secure_client);j=create(secure_client,a)
    r=secure_client.post('/v1/deliverables/'+j['job_id']+'/generate',headers=a,json={'kind':'budget_xlsx'})
    assert r.status_code==422,r.text
    assert r.json()['detail']['code']=='FORMAL_DELIVERABLE_BLOCKED'
    assert secure_client.get('/v1/deliverables/'+j['job_id'],headers=a).json()['formal_status']=='BLOCKED'
