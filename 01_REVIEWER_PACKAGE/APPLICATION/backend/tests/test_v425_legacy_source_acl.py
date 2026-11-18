from .test_v24_api_auth import secure_client,login

def test_legacy_source_route_cannot_bypass_project_scope(secure_client,monkeypatch):
    import txbuddy.pilot.api as extension
    monkeypatch.setattr(extension,'source_detail',lambda db,id:{'document':{'local_path':'05_REAL_PROJECTS/L1_BUDGET/L1_001/private.pdf'}})
    headers=login(secure_client)
    r=secure_client.get('/v1/sources/TEST_ONLY_PRIVATE',headers=headers)
    assert r.status_code==403
    assert r.json()['detail']=={'code':'ACL_FORBIDDEN','message':'无权访问该项目'}
