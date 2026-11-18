"""Real HTTP requests against the app; synthetic OCR in disposable SQLite only."""
import pytest
from txbuddy import api
from txbuddy.governance.ocr_review import ReviewStore
from txbuddy.security.users import UserDirectory
from .test_v24_api_auth import secure_client, login, create
from .test_v425_ocr_review import draft


@pytest.fixture
def acl_review(secure_client, tmp_path, monkeypatch, draft):
    users = ('owner', 'editor', 'reviewer', 'viewer', 'outsider')
    monkeypatch.setattr(api.app.state.security, 'directory', UserDirectory.from_plaintext({
        name: {'password': name + '-test-password', 'roles': ('estimator',)}
        for name in users
    }))
    headers = {name: login(secure_client, name) for name in users}
    create(secure_client, headers['owner'], 'V426-ACL')
    for name in ('editor', 'reviewer', 'viewer'):
        api.app.state.security.acl.grant('V426-ACL', name, name.upper(), granted_by='owner')
    store = ReviewStore(tmp_path / 'ocr-v426-TEST_ONLY.sqlite3')
    monkeypatch.setattr(api.app.state, 'review_store', store)
    imported = store.ingest(draft[2], 'V426-ACL', 'TEST_ONLY.pdf', draft[0], draft[1])
    page = store.page(store.document(imported['document_id'])['pages'][0]['page_id'])
    return secure_client, store, headers, page


def legacy_body(page, **changes):
    return {'expected_version': page['version'], 'corrected_text': page['corrected_text'],
            'structured_fields': page['structured_fields'], 'review_status': 'IN_REVIEW',
            'reason': 'TEST_ONLY API authorization check', **changes}


def decision_body(page, decision='APPROVE', **changes):
    return {'expected_version': page['version'],
            'expected_content_sha256': page.get('content_sha256', '0' * 64),
            'submitted_version': page.get('submitted_version') or page['version'] or 1,
            'decision': decision, 'reason': 'TEST_ONLY decision', 'human_confirmed': True, **changes}


def submit(fixture):
    client, store, headers, original = fixture
    response = client.post('/v1/governance/ocr/pages/' + original['page_id'] + '/reviews',
                           headers=headers['editor'], json=legacy_body(original))
    assert response.status_code == 200, response.text
    saved = response.json()
    response = client.post('/v1/governance/ocr/pages/' + original['page_id'] + '/submit',
        headers=headers['editor'], json={'expected_version': saved['version'],
            'expected_content_sha256': saved.get('content_sha256', '0' * 64)})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('role', ['viewer', 'editor', 'reviewer', 'owner'])
def test_every_member_role_can_read_real_api(acl_review, role):
    client, store, headers, page = acl_review
    base = '/v1/governance/ocr/pages/' + page['page_id']
    assert client.get(base, headers=headers[role]).status_code == 200
    assert client.get(base + '/image', headers=headers[role]).status_code == 200


@pytest.mark.parametrize('role,allowed', [('viewer', False), ('editor', True), ('reviewer', False), ('owner', True)])
def test_legacy_submit_permission_is_write_not_role_level(acl_review, role, allowed):
    client, store, headers, page = acl_review
    response = client.post('/v1/governance/ocr/pages/' + page['page_id'] + '/reviews',
                           headers=headers[role], json=legacy_body(page, corrected_text='TEST_ONLY changed text'))
    assert response.status_code == (200 if allowed else 403), response.text
    assert store.page(page['page_id'])['version'] == int(allowed)


@pytest.mark.parametrize('role,allowed', [('viewer', False), ('editor', False), ('reviewer', True), ('owner', True)])
@pytest.mark.parametrize('decision,status', [('APPROVE', 'REVIEWED'), ('RETURN', 'RETURNED')])
def test_real_api_decision_permission_matrix(acl_review, role, allowed, decision, status):
    client, store, headers, original = acl_review
    submitted = submit(acl_review)
    response = client.post('/v1/governance/ocr/pages/' + original['page_id'] + '/decisions',
                           headers=headers[role], json=decision_body(submitted, decision))
    assert response.status_code == (200 if allowed else 403), response.text
    current = store.page(original['page_id'])
    assert current['version'] == (3 if allowed else 2)
    if allowed:
        actual = response.json()
        assert actual['review_status'] == status
        assert actual['corrected_text'] == submitted['corrected_text']
        assert actual['change_history'][-1]['reviewer_id'] == role
        assert actual['history_chain_valid']


def test_editor_cannot_approve_by_forging_legacy_status(acl_review):
    client, store, headers, original = acl_review
    submitted = submit(acl_review)
    response = client.post('/v1/governance/ocr/pages/' + original['page_id'] + '/reviews',
        headers=headers['editor'], json=legacy_body(submitted, review_status='REVIEWED', human_confirmed=True))
    assert response.status_code == 403, response.text
    assert store.page(original['page_id'])['version'] == 2


def test_reviewer_can_approve_legacy_submitted_exact_content(acl_review):
    client, store, headers, original = acl_review
    submitted = submit(acl_review)
    response = client.post('/v1/governance/ocr/pages/' + original['page_id'] + '/reviews',
        headers=headers['reviewer'], json=legacy_body(submitted, review_status='REVIEWED', human_confirmed=True,
            expected_content_sha256=submitted.get('content_sha256', '0' * 64),
            submitted_version=submitted.get('submitted_version', submitted['version'])))
    assert response.status_code == 200, response.text
    assert response.json()['corrected_text'] == submitted['corrected_text']


@pytest.mark.parametrize('field,value', [('corrected_text', 'unauthorized edit'), ('structured_fields', {'forged': True})])
def test_reviewer_cannot_change_content_through_legacy_decision(acl_review, field, value):
    client, store, headers, original = acl_review
    submitted = submit(acl_review)
    body = legacy_body(submitted, review_status='REVIEWED', human_confirmed=True,
        expected_content_sha256=submitted.get('content_sha256', '0' * 64),
        submitted_version=submitted.get('submitted_version', submitted['version']))
    body[field] = value
    response = client.post('/v1/governance/ocr/pages/' + original['page_id'] + '/reviews',
                           headers=headers['reviewer'], json=body)
    assert response.status_code in (403, 409, 422), response.text
    assert store.page(original['page_id'])['version'] == 2


@pytest.mark.parametrize('field,value', [('corrected_text', 'forged'), ('reviewer_id', 'owner'),
                                       ('professional', {'license_number': 'fake'}), ('review_status', 'REVIEWED')])
def test_decision_endpoint_rejects_untrusted_fields(acl_review, field, value):
    client, store, headers, original = acl_review
    submitted = submit(acl_review)
    response = client.post('/v1/governance/ocr/pages/' + original['page_id'] + '/decisions',
        headers=headers['reviewer'], json=decision_body(submitted, **{field: value}))
    assert response.status_code == 422, response.text
    assert store.page(original['page_id'])['version'] == 2


def test_decision_requires_submitted_content_not_draft(acl_review):
    client, store, headers, original = acl_review
    base = '/v1/governance/ocr/pages/' + original['page_id']
    draft_response = client.post(base + '/drafts', headers=headers['editor'],
        json={'expected_version': 0, 'corrected_text': 'TEST_ONLY draft', 'structured_fields': {}})
    assert draft_response.status_code == 200, draft_response.text
    response = client.post(base + '/decisions', headers=headers['reviewer'], json=decision_body(draft_response.json()))
    assert response.status_code == 409, response.text
    submitted = client.post(base + '/submit', headers=headers['editor'], json={
        'expected_version': 1, 'expected_content_sha256': draft_response.json()['content_sha256']})
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()['review_status'] == 'IN_REVIEW'


@pytest.mark.parametrize('kind', ['hash', 'version'])
def test_decision_bound_to_exact_submitted_snapshot(acl_review, kind):
    client, store, headers, original = acl_review
    submitted = submit(acl_review)
    changes = {'expected_content_sha256': 'f' * 64} if kind == 'hash' else {'expected_version': 0}
    response = client.post('/v1/governance/ocr/pages/' + original['page_id'] + '/decisions',
        headers=headers['reviewer'], json=decision_body(submitted, **changes))
    assert response.status_code == 409, response.text
    assert store.page(original['page_id'])['version'] == 2


def test_editing_approved_content_invalidates_previous_decision(acl_review):
    client, store, headers, original = acl_review
    submitted = submit(acl_review)
    base = '/v1/governance/ocr/pages/' + original['page_id']
    approved = client.post(base + '/decisions', headers=headers['reviewer'], json=decision_body(submitted))
    assert approved.status_code == 200, approved.text
    edited = client.post(base + '/drafts', headers=headers['editor'],
        json={'expected_version': 3, 'corrected_text': 'TEST_ONLY revised after decision', 'structured_fields': {}})
    assert edited.status_code == 200, edited.text
    result = edited.json()
    assert result['review_status'] == 'NEEDS_EXPERT_REVIEW'
    assert result['content_sha256'] != submitted['content_sha256']
    stale = client.post(base + '/decisions', headers=headers['reviewer'], json=decision_body(submitted))
    assert stale.status_code == 409, stale.text


def test_return_requires_reason_and_approval_requires_human_attestation(acl_review):
    client, store, headers, original = acl_review
    submitted = submit(acl_review)
    base = '/v1/governance/ocr/pages/' + original['page_id']
    assert client.post(base + '/decisions', headers=headers['reviewer'],
        json=decision_body(submitted, 'RETURN', reason='   ')).status_code == 422
    assert client.post(base + '/decisions', headers=headers['reviewer'],
        json=decision_body(submitted, human_confirmed=False)).status_code == 422
    assert store.page(original['page_id'])['version'] == 2


@pytest.mark.parametrize('role', ['owner', 'reviewer', 'editor'])
def test_professional_signoff_requires_extra_server_identity(acl_review, role):
    client, store, headers, page = acl_review
    response = client.post('/v1/governance/ocr/pages/' + page['page_id'] + '/signoff',
                           headers=headers[role], json=legacy_body(page, human_confirmed=True))
    assert response.status_code == 403, response.text
    assert store.page(page['page_id'])['version'] == 0


def test_outsider_cannot_read_or_write_any_ocr_endpoint(acl_review):
    client, store, headers, original = acl_review
    submitted = submit(acl_review)
    base = '/v1/governance/ocr/pages/' + original['page_id']
    for suffix in ('', '/image'):
        assert client.get(base + suffix, headers=headers['outsider']).status_code == 403
    assert client.get('/v1/governance/ocr/documents/' + original['document_id'], headers=headers['outsider']).status_code == 403
    for suffix, body in (('/reviews', legacy_body(submitted)), ('/drafts', {
            'expected_version': 1, 'corrected_text': 'cross project', 'structured_fields': {}}),
            ('/submit', {'expected_version': 1, 'expected_content_sha256': submitted.get('content_sha256', '0' * 64)}),
            ('/decisions', decision_body(submitted))):
        assert client.post(base + suffix, headers=headers['outsider'], json=body).status_code == 403
    assert client.get('/v1/governance/ocr', headers=headers['outsider']).json()['total'] == 0
    assert store.page(original['page_id'])['version'] == 2


@pytest.mark.parametrize('authorization', ['', 'Bearer invalid.token.value', 'Bearer forged'])
def test_invalid_token_never_reaches_ocr_action(acl_review, authorization):
    client, store, headers, original = acl_review
    base = '/v1/governance/ocr/pages/' + original['page_id']
    for suffix, body in (('/reviews', legacy_body(original)), ('/decisions', decision_body(original))):
        response = client.post(base + suffix, headers={'Authorization': authorization}, json=body)
        assert response.status_code == 401, response.text
    assert store.page(original['page_id'])['version'] == 0


def test_owner_claim_registry_is_not_filled_from_memberships(acl_review):
    client, store, headers, original = acl_review
    acl = api.app.state.security.acl
    acl.grant('MEMBERSHIP-ONLY', 'owner', 'OWNER', granted_by='TEST_ONLY')
    client.get('/v1/governance/ocr', headers=headers['owner'])
    with acl.connect() as connection:
        assert connection.execute('SELECT count(*) FROM acl_project WHERE project_id=?', ('MEMBERSHIP-ONLY',)).fetchone()[0] == 0
