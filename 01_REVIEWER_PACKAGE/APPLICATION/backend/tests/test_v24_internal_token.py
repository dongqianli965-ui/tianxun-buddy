from .test_v24_api_auth import secure_client
import pytest


@pytest.mark.parametrize('configured',[None,'','dev-internal','txbuddy-dev-internal-token-change-me'])
def test_internal_fail_closed(secure_client,monkeypatch,configured):
    if configured is None: monkeypatch.delenv('TX_INTERNAL_TOKEN',raising=False)
    else: monkeypatch.setenv('TX_INTERNAL_TOKEN',configured)
    r=secure_client.post('/internal/job/missing/records',json={'kind':'x','payload':{}},headers={'X-Internal-Token':configured or 'dev-internal'})
    assert r.status_code==403,r.text


def test_rotation_rejects_previous_secret(secure_client,monkeypatch):
    monkeypatch.setenv('TX_INTERNAL_TOKEN','rotated-v24-test-secret')
    for token in ('','dev-internal','previous-v24-test-secret'):
        assert secure_client.post('/internal/job/missing/records',json={'kind':'x','payload':{}},headers={'X-Internal-Token':token}).status_code==403

def test_correct_token_writes_and_rotation_revokes_old_value(secure_client,monkeypatch):
    from .test_v24_api_auth import login,create
    job=create(secure_client,login(secure_client));path='/internal/job/'+job['job_id']+'/records';body={'kind':'boq','payload':{'test_only':True}}
    monkeypatch.setenv('TX_INTERNAL_TOKEN','v424-rotation-first-test-secret')
    assert secure_client.post(path,json=body,headers={'X-Internal-Token':'v424-rotation-first-test-secret'}).status_code==200
    monkeypatch.setenv('TX_INTERNAL_TOKEN','v424-rotation-second-test-secret')
    assert secure_client.post(path,json=body,headers={'X-Internal-Token':'v424-rotation-first-test-secret'}).status_code==403
    assert secure_client.post(path,json=body,headers={'X-Internal-Token':'v424-rotation-second-test-secret'}).status_code==200
