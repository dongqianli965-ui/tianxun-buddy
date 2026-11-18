from .test_v24_api_auth import secure_client,login,create
def test_jobs_filter_before_pagination(secure_client):
    a=login(secure_client);create(secure_client,a,'ALICE-1');create(secure_client,a,'ALICE-2');b=login(secure_client,'mallory');create(secure_client,b,'MALLORY-1')
    r=secure_client.get('/v1/jobs?limit=1&offset=1',headers=a).json();assert r['total']==2 and len(r['items'])==1
    assert secure_client.get('/v1/jobs?project_id=MALLORY-1',headers=a).json()['total']==0
